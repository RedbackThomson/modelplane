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

"""Compose a MetricMapping.

A MetricMapping names the metrics one component emits and what Modelplane
calls them. compose-serving-stack compiles every mapping into the transform
processor of the collector on each inference cluster.

This function composes nothing. The collector is composed by
compose-serving-stack, which reads every MetricMapping. What this function
does is tell an operator whether the mapping reaches anything, because a
mapping that reaches no cluster looks identical to one that works.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.metricmapping import v1alpha1

CONDITION_TYPE_ACCEPTED = "Accepted"
CONDITION_REASON_AVAILABLE = "Available"
CONDITION_REASON_WAITING_FOR_CLUSTERS = "WaitingForClusters"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"


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
        xr = v1alpha1.MetricMapping(**resource.struct_to_dict(req.observed.composite.resource))

        # Every inference cluster renders every mapping, so the count of
        # clusters is the count that took these renames.
        response.require_resources(
            rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
        )
        if "clusters" not in req.required_resources:
            _not_ready(rsp, CONDITION_REASON_WAITING_FOR_CLUSTERS, "Waiting for the inference clusters to resolve")
            return rsp

        clusters = len(list(request.get_required_resources(req, "clusters")))
        resource.update_status(rsp.desired.composite, v1alpha1.Status(clusters=clusters))

        if clusters == 0:
            _not_ready(rsp, CONDITION_REASON_NO_CLUSTERS, "No inference cluster to render these renames into")
            return rsp

        response.set_conditions(
            rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ACCEPTED,
                status="True",
                reason=CONDITION_REASON_AVAILABLE,
                message=f"Renaming {len(xr.spec.metrics)} metric(s) on {clusters} inference cluster(s)",
            ),
        )
        rsp.desired.composite.ready = fnv1.READY_TRUE
        return rsp


def _not_ready(rsp: fnv1.RunFunctionResponse, reason: str, message: str) -> None:
    """Report a mapping that isn't reaching anything, and why."""
    response.set_conditions(
        rsp,
        resource.Condition(typ=CONDITION_TYPE_ACCEPTED, status="False", reason=reason, message=message),
    )
    rsp.desired.composite.ready = fnv1.READY_FALSE
