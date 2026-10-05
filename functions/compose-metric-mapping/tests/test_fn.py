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
from models.ai.modelplane.metricmapping import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-metric-mapping."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _metric_mapping() -> fnv1.Resource:
    """The my-engine MetricMapping XR, renaming my_engine_queued to modelplane_requests_waiting."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.MetricMapping(
                metadata=metav1.ObjectMeta(name="my-engine"),
                spec=v1alpha1.Spec(
                    # The model accepts its from_ field only by its alias,
                    # from, which is a Python keyword, so the Metric is
                    # validated from its wire form.
                    metrics=[
                        v1alpha1.Metric.model_validate(
                            {"from": "my_engine_queued", "to": "modelplane_requests_waiting"}
                        ),
                    ],
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_metric_mapping(*, status: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired MetricMapping XR, carrying status, or only its readiness if status is None."""
    if status is None:
        return fnv1.Resource(ready=ready)
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}), ready=ready)


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# This function composes nothing, so desired carries only the composite.
COMPOSE_CASES = [
    # The function counts the clusters without reading them, so the two are
    # identical.
    Case(
        name="TwoClusters",
        reason="With two inference clusters resolved, the mapping is Accepted and Ready, and its status counts both.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_metric_mapping()),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceCluster",
                                    "metadata": {"name": "prod-us-east"},
                                }
                            ),
                        ),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceCluster",
                                    "metadata": {"name": "prod-us-east"},
                                }
                            ),
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_metric_mapping(status={"clusters": 2}, ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Renaming 1 metric(s) on 2 inference cluster(s)",
                ),
            ],
        ),
    ),
    Case(
        name="NoClusters",
        reason="With the clusters requirement resolved to none, the mapping isn't Ready and its status counts no clusters.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_metric_mapping()),
            required_resources={"clusters": fnv1.Resources()},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_metric_mapping(status={"clusters": 0}, ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoClusters",
                    message="No inference cluster to render these renames into",
                ),
            ],
        ),
    ),
    Case(
        name="ClustersUnresolved",
        reason="Until the clusters requirement resolves, the mapping waits for it, not Ready and with no status.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_metric_mapping()),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_metric_mapping(status=None, ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForClusters",
                    message="Waiting for the inference clusters to resolve",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction reports whether the mapping reaches any inference cluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
