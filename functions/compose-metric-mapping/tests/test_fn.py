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
        """The function reports whether a mapping reaches any cluster."""
        mapping = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "MetricMapping",
            "metadata": {"name": "my-engine"},
            "spec": {
                "statements": ['set(name, "modelplane_requests_waiting") where name == "my_engine_queued"'],
            },
        }
        cluster = resource.dict_to_struct(
            {"apiVersion": "modelplane.ai/v1alpha1", "kind": "InferenceCluster", "metadata": {"name": "prod-us-east"}}
        )

        def req(xr: dict, clusters: list | None) -> fnv1.RunFunctionRequest:
            r = fnv1.RunFunctionRequest(
                observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(xr))),
            )
            if clusters is not None:
                r.required_resources["clusters"].items.extend([fnv1.Resource(resource=c) for c in clusters])
            return r

        def want(ready: fnv1.Ready, status: dict | None, cond: fnv1.Condition) -> fnv1.RunFunctionResponse:
            composite = fnv1.Resource(ready=ready)
            if status is not None:
                composite.resource.CopyFrom(resource.dict_to_struct(status))
            return fnv1.RunFunctionResponse(
                meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                desired=fnv1.State(composite=composite),
                conditions=[cond],
                context=structpb.Struct(),
                requirements=fnv1.Requirements(
                    resources={
                        "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster")
                    }
                ),
            )

        no_statements = {**mapping, "spec": {}}

        cases = [
            Case(
                name="ready, and says how many clusters took the statements",
                req=req(mapping, [cluster, cluster]),
                want=want(
                    fnv1.READY_TRUE,
                    {"status": {"clusters": 2}},
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_TRUE,
                        reason="Available",
                        message="Rendered into 2 inference cluster(s)",
                    ),
                ),
            ),
            Case(
                name="not ready when no cluster exists to render into",
                req=req(mapping, []),
                want=want(
                    fnv1.READY_FALSE,
                    {"status": {"clusters": 0}},
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_FALSE,
                        reason="NoClusters",
                        message="No inference cluster to render these statements into",
                    ),
                ),
            ),
            Case(
                name="not ready when the mapping would change nothing",
                req=req(no_statements, [cluster]),
                want=want(
                    fnv1.READY_FALSE,
                    {"status": {"clusters": 1}},
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_FALSE,
                        reason="NoStatements",
                        message="No statements, so this mapping changes nothing",
                    ),
                ),
            ),
            Case(
                name="waits for the clusters to resolve",
                req=req(mapping, None),
                want=want(
                    fnv1.READY_FALSE,
                    None,
                    fnv1.Condition(
                        type="Accepted",
                        status=fnv1.STATUS_CONDITION_FALSE,
                        reason="WaitingForClusters",
                        message="Waiting for the inference clusters to resolve",
                    ),
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
