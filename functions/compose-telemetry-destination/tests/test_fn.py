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

"""Tests for the compose-metric-mapping function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb


@dataclasses.dataclass
class Case:
    """A test case for compose-metric-mapping."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function reports whether a destination can actually be sent through."""
        exporters = {
            "otlphttp": {"endpoint": "https://otel.acme.example", "auth": {"authenticator": "bearertokenauth"}}
        }
        extensions = {"bearertokenauth": {"token": "${env:OTLP_TOKEN}"}}

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
                r.required_resources["secret"].items.extend([fnv1.Resource(resource=s) for s in secrets])
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
                rsp.requirements.resources["secret"].api_version = "v1"
                rsp.requirements.resources["secret"].kind = "Secret"
                rsp.requirements.resources["secret"].match_name = secret
            return rsp

        cases = [
            Case(
                name="ready, naming the exporters it sends through",
                req=req({"exporters": exporters, "extensions": extensions}),
                want=want(
                    fnv1.READY_TRUE,
                    {"status": {}},
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_TRUE,
                        reason="Available",
                        message="Exporting through otlphttp",
                    ),
                ),
            ),
            Case(
                name="ready with an exporter that references no authenticator at all",
                req=req(
                    {"exporters": {"prometheusremotewrite": {"endpoint": "https://prom.acme.example/api/v1/write"}}}
                ),
                want=want(
                    fnv1.READY_TRUE,
                    {"status": {}},
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_TRUE,
                        reason="Available",
                        message="Exporting through prometheusremotewrite",
                    ),
                ),
            ),
            Case(
                name="ready once the credential Secret exists",
                req=req(
                    {"exporters": exporters, "extensions": extensions, "secretRef": {"name": "telemetry-credentials"}},
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
                        message="Exporting through otlphttp",
                    ),
                    secret="telemetry-credentials",
                ),
            ),
            Case(
                name="waits for the credential Secret to resolve",
                req=req(
                    {"exporters": exporters, "extensions": extensions, "secretRef": {"name": "telemetry-credentials"}},
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
                name="not ready when an exporter names an authenticator nothing defines",
                req=req({"exporters": exporters}),
                want=want(
                    fnv1.READY_FALSE,
                    None,
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_FALSE,
                        reason="UnknownAuthenticator",
                        message="No extension defines bearertokenauth, so the collector would refuse to start",
                    ),
                ),
            ),
            Case(
                name="an exporter that is not a mapping is left to the collector to reject",
                req=req({"exporters": {"otlphttp": "https://otel.acme.example"}}),
                want=want(
                    fnv1.READY_TRUE,
                    {"status": {}},
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_TRUE,
                        reason="Available",
                        message="Exporting through otlphttp",
                    ),
                ),
            ),
            Case(
                name="not ready with no exporters at all",
                req=req({"exporters": {}}),
                want=want(
                    fnv1.READY_FALSE,
                    None,
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_FALSE,
                        reason="NoExporters",
                        message="No exporters, so collected telemetry has nowhere to go",
                    ),
                ),
            ),
            Case(
                name="not ready when the credential Secret is missing",
                req=req(
                    {"exporters": exporters, "extensions": extensions, "secretRef": {"name": "telemetry-credentials"}},
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
                            "Secret telemetry-credentials does not exist, "
                            "so the collector has no credential to send with"
                        ),
                    ),
                    secret="telemetry-credentials",
                ),
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
