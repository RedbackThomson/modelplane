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
    """A test case for compose-metric-mapping."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


def _compose_cases() -> list[Case]:
    mapping = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "MetricMapping",
        "metadata": {"name": "my-engine"},
        "spec": {
            "metrics": [{"from": "my_engine_queued", "to": "modelplane_requests_waiting"}],
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

    return [
        Case(
            name="ready, and says how many clusters took the renames",
            req=req(mapping, [cluster, cluster]),
            want=want(
                fnv1.READY_TRUE,
                {"status": {"clusters": 2}},
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Renaming 1 metric(s) on 2 inference cluster(s)",
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
                    message="No inference cluster to render these renames into",
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


@pytest.mark.parametrize("case", _compose_cases(), ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """The function reports whether a mapping reaches any cluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
