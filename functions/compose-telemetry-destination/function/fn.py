# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compose a TelemetryDestination.

A TelemetryDestination carries the collector's exporters and extensions
verbatim, and compose-serving-stack renders them into the collector it
composes. Modelplane does not model what an exporter is, so there is little
here to validate and the little there is matters: an exporter naming an
authenticator that no extension defines makes a collector refuse to start,
and that failure surfaces as telemetry silently never arriving.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.telemetrydestination import v1alpha1

CONDITION_TYPE_ACCEPTED = "Accepted"
CONDITION_REASON_AVAILABLE = "Available"
CONDITION_REASON_NO_EXPORTERS = "NoExporters"
CONDITION_REASON_UNKNOWN_AUTHENTICATOR = "UnknownAuthenticator"
CONDITION_REASON_WAITING_FOR_SECRET = "WaitingForSecret"
CONDITION_REASON_SECRET_NOT_FOUND = "SecretNotFound"

_SECRET_KEY = "secret"


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        xr = v1alpha1.TelemetryDestination(**resource.struct_to_dict(req.observed.composite.resource))

        exporters = xr.spec.exporters or {}
        if not exporters:
            _not_ready(rsp, CONDITION_REASON_NO_EXPORTERS, "No exporters, so collected telemetry has nowhere to go")
            return rsp

        # An exporter's auth block names an authenticator by extension name. The
        # collector refuses to start when it names one no extension defines, and
        # a collector that never starts looks exactly like a fleet that produces
        # nothing, so it is worth catching on the object instead.
        extensions = set((xr.spec.extensions or {}).keys())
        missing = sorted(_authenticators(exporters) - extensions)
        if missing:
            _not_ready(
                rsp,
                CONDITION_REASON_UNKNOWN_AUTHENTICATOR,
                f"No extension defines {', '.join(missing)}, so the collector would refuse to start",
            )
            return rsp

        if xr.spec.secretRef is not None:
            response.require_resources(
                rsp,
                name=_SECRET_KEY,
                api_version="v1",
                kind="Secret",
                match_name=xr.spec.secretRef.name,
            )
            if _SECRET_KEY not in req.required_resources:
                _not_ready(rsp, CONDITION_REASON_WAITING_FOR_SECRET, "Waiting for the credential Secret to resolve")
                return rsp
            if not list(request.get_required_resources(req, _SECRET_KEY)):
                _not_ready(
                    rsp,
                    CONDITION_REASON_SECRET_NOT_FOUND,
                    f"Secret {xr.spec.secretRef.name} does not exist, so the collector has no credential to send with",
                )
                return rsp

        resource.update_status(rsp.desired.composite, v1alpha1.Status())
        response.set_conditions(
            rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ACCEPTED,
                status="True",
                reason=CONDITION_REASON_AVAILABLE,
                message=f"Exporting through {', '.join(sorted(exporters))}",
            ),
        )
        rsp.desired.composite.ready = fnv1.READY_TRUE
        return rsp


def _authenticators(exporters: dict) -> set[str]:
    """Every authenticator an exporter references, by extension name."""
    names: set[str] = set()
    for cfg in exporters.values():
        if not isinstance(cfg, dict):
            continue
        auth = cfg.get("auth")
        if isinstance(auth, dict) and isinstance(auth.get("authenticator"), str):
            names.add(auth["authenticator"])
    return names


def _not_ready(rsp: fnv1.RunFunctionResponse, reason: str, message: str) -> None:
    """Report a destination nothing can send through, and why."""
    response.set_conditions(
        rsp,
        resource.Condition(typ=CONDITION_TYPE_ACCEPTED, status="False", reason=reason, message=message),
    )
    rsp.desired.composite.ready = fnv1.READY_FALSE
