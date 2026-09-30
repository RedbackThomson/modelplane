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

A TelemetryDestination names the sinks the fleet's metrics go to, each
carrying an exporter's own configuration verbatim, and compose-serving-stack
renders them into the collector it composes. Modelplane does not model what an
exporter is, so there is little here to validate and the little there is
matters: a sink naming an authenticator that nothing defines makes a collector
refuse to start, and that failure surfaces as telemetry silently never
arriving.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.telemetrydestination import v1alpha1

CONDITION_TYPE_ACCEPTED = "Accepted"
CONDITION_REASON_AVAILABLE = "Available"
CONDITION_REASON_UNKNOWN_AUTHENTICATOR = "UnknownAuthenticator"
CONDITION_REASON_WAITING_FOR_SECRET = "WaitingForSecret"
CONDITION_REASON_SECRET_NOT_FOUND = "SecretNotFound"

_SECRET_PREFIX = "secret-"


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

        sinks = list(xr.spec.sinks)

        # A sink's auth block names an authenticator by extension name. The
        # collector refuses to start when it names one no extension defines, and
        # a collector that never starts looks exactly like a fleet that produces
        # nothing, so it is worth catching on the object instead.
        # An authenticator is defined either by the operator, under extensions,
        # or by Modelplane, for a sink that set auth. Both count.
        defined = set((xr.spec.extensions or {}).keys())
        defined |= {f"bearertokenauth/{s.name}" for s in sinks if s.auth and s.auth.bearerTokenKey}
        missing = sorted(_authenticators(sinks) - defined)
        if missing:
            _not_ready(
                rsp,
                CONDITION_REASON_UNKNOWN_AUTHENTICATOR,
                f"No extension defines {', '.join(missing)}, so the collector would refuse to start",
            )
            return rsp

        for sink in sinks:
            if sink.secretRef is None:
                continue
            key = f"{_SECRET_PREFIX}{sink.name}"
            response.require_resources(
                rsp,
                name=key,
                api_version="v1",
                kind="Secret",
                match_name=sink.secretRef.name,
            )
            if key not in req.required_resources:
                _not_ready(rsp, CONDITION_REASON_WAITING_FOR_SECRET, "Waiting for the credential Secret to resolve")
                return rsp
            if not list(request.get_required_resources(req, key)):
                _not_ready(
                    rsp,
                    CONDITION_REASON_SECRET_NOT_FOUND,
                    f"Secret {sink.secretRef.name} does not exist, so sink {sink.name} has no credential to send with",
                )
                return rsp

        resource.update_status(rsp.desired.composite, v1alpha1.Status())
        response.set_conditions(
            rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ACCEPTED,
                status="True",
                reason=CONDITION_REASON_AVAILABLE,
                message=f"Exporting through {', '.join(f'{s.type}/{s.name}' for s in sinks)}",
            ),
        )
        rsp.desired.composite.ready = fnv1.READY_TRUE
        return rsp


def _authenticators(sinks: list[v1alpha1.Sink]) -> set[str]:
    """Every authenticator a sink's own config references, by extension name."""
    names: set[str] = set()
    for sink in sinks:
        auth = (sink.config or {}).get("auth")
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
