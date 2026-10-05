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

"""Tests that the InferenceGateway meters what its callers use."""

from e2e import gateway, kube, wait


def test_usage_record(serving: gateway.Serving, control_plane_client: gateway.Client, workload: kube.Cluster) -> None:
    """The InferenceGateway's access log attributes a request's tokens to its caller.

    The mock engine reports the same tokens for every request, so every request
    the tests send logs an identical record. This counts the matching records
    before sending a request, and waits for the count to grow.
    """
    # The endpoint is the ModelRoute's backend for the ModelEndpoint, in
    # ml-team's mirrored namespace on the workload cluster.
    want = {
        "caller": "e2e",
        "service": "ml-team/mock",
        "endpoint": "mp-ml-team-51733/mock-local-934fc-mock-demo-da96c-f8a13",
        "served_model": "ml-team/mock-demo",
        "input_tokens": 12,
        "output_tokens": 9,
        "total_tokens": 21,
        "status": 200,
    }

    def matching() -> int:
        return sum(1 for record in gateway.usage_records(workload) if {k: record.get(k) for k in want} == want)

    before = matching()
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        gateway.BEARER,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200

    def logged() -> None:
        assert matching() > before, f"no new usage record matching {want}"

    wait.until(logged, timeout=60, what="the InferenceGateway to log the request", retry=kube.RETRY)
