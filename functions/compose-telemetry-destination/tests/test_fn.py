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

"""Tests for the compose-telemetry-destination function."""

import asyncio
import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb


@dataclasses.dataclass
class Case:
    """A test case for compose-telemetry-destination."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


def _compose_cases() -> list[Case]:
    def sink(name: str = "primary", type_: str = "otlphttp", secret: str | None = None) -> dict:
        """A sink wiring its own authenticator, which is the case worth validating."""
        return {
            "name": name,
            "type": type_,
            "endpoint": "https://otel.acme.example",
            "config": {"auth": {"authenticator": "oauth2client/acme"}},
            **({"secretRef": {"name": secret}} if secret else {}),
        }

    sinks = [sink()]
    extensions = {"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}}

    def xr(spec: dict) -> dict:
        return {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "TelemetryDestination",
            "metadata": {"name": "default"},
            "spec": spec,
        }

    def req(spec: dict, secrets: list | None = None) -> fnv1.RunFunctionRequest:
        r = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(xr(spec)))),
        )
        if secrets is not None:
            r.required_resources["secret-primary"].items.extend([fnv1.Resource(resource=s) for s in secrets])
        return r

    def want(
        ready: fnv1.Ready, status: dict | None, cond: fnv1.Condition, secret: str | None = None
    ) -> fnv1.RunFunctionResponse:
        composite = fnv1.Resource(ready=ready)
        if status is not None:
            composite.resource.CopyFrom(resource.dict_to_struct(status))
        rsp = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=composite),
            conditions=[cond],
            context=structpb.Struct(),
        )
        if secret is not None:
            rsp.requirements.resources["secret-primary"].api_version = "v1"
            rsp.requirements.resources["secret-primary"].kind = "Secret"
            rsp.requirements.resources["secret-primary"].match_name = secret
            # Qualified: unqualified it would resolve a Secret of that
            # name in any namespace, and accept the wrong credential.
            rsp.requirements.resources["secret-primary"].namespace = "modelplane-system"
        return rsp

    return [
        Case(
            name="ready, naming the sinks it sends through",
            req=req({"sinks": sinks, "extensions": extensions}),
            want=want(
                fnv1.READY_TRUE,
                {"status": {}},
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through otlphttp/primary",
                ),
            ),
        ),
        Case(
            name="ready with an exporter that references no authenticator at all",
            req=req(
                {
                    "sinks": [
                        {
                            "name": "prom",
                            "type": "prometheusremotewrite",
                            "endpoint": "https://prom.acme.example/api/v1/write",
                        }
                    ]
                }
            ),
            want=want(
                fnv1.READY_TRUE,
                {"status": {}},
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through prometheusremotewrite/prom",
                ),
            ),
        ),
        Case(
            name="ready with no extensions, because Modelplane composes the authenticator",
            req=req(
                {
                    "sinks": [
                        {
                            "name": "primary",
                            "type": "otlphttp",
                            "endpoint": "https://otel.acme.example",
                            "secretRef": {"name": "telemetry-credentials"},
                            "auth": {"bearerTokenKey": "token"},
                        }
                    ]
                },
                secrets=[
                    resource.dict_to_struct(
                        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "telemetry-credentials"}}
                    )
                ],
            ),
            want=want(
                fnv1.READY_TRUE,
                {"status": {}},
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through otlphttp/primary",
                ),
                secret="telemetry-credentials",
            ),
        ),
        Case(
            name="ready once the credential Secret exists",
            req=req(
                {"sinks": [sink(secret="telemetry-credentials")], "extensions": extensions},
                secrets=[
                    resource.dict_to_struct(
                        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "telemetry-credentials"}}
                    )
                ],
            ),
            want=want(
                fnv1.READY_TRUE,
                {"status": {}},
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through otlphttp/primary",
                ),
                secret="telemetry-credentials",
            ),
        ),
        Case(
            name="waits for the credential Secret to resolve",
            req=req(
                {"sinks": [sink(secret="telemetry-credentials")], "extensions": extensions},
            ),
            want=want(
                fnv1.READY_FALSE,
                None,
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForSecret",
                    message="Waiting for the credential Secret to resolve",
                ),
                secret="telemetry-credentials",
            ),
        ),
        Case(
            name="not ready when a sink names an authenticator nothing defines",
            req=req({"sinks": sinks}),
            want=want(
                fnv1.READY_FALSE,
                None,
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="UnknownAuthenticator",
                    message="No extension defines oauth2client/acme, so the collector would refuse to start",
                ),
            ),
        ),
        Case(
            name="not ready when the credential Secret is missing",
            req=req(
                {"sinks": [sink(secret="telemetry-credentials")], "extensions": extensions},
                secrets=[],
            ),
            want=want(
                fnv1.READY_FALSE,
                None,
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="SecretNotFound",
                    message=(
                        "Secret telemetry-credentials does not exist, so sink primary has no credential to send with"
                    ),
                ),
                secret="telemetry-credentials",
            ),
        ),
    ]


@pytest.mark.parametrize("case", _compose_cases(), ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """The function reports whether a destination can actually be sent through."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
