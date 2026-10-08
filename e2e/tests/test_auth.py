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

"""Tests that the InferenceGateway authenticates its callers."""

import dataclasses

import pytest

from e2e import gateway


@dataclasses.dataclass
class Case:
    """A request the InferenceGateway refuses."""

    name: str
    reason: str
    headers: dict[str, str]
    want: int


REFUSED_CASES = [
    Case(
        name="NoKey",
        reason="The InferenceGateway refuses a caller that presents no key.",
        headers={},
        want=401,
    ),
    Case(
        name="UnknownKey",
        reason="The InferenceGateway refuses a caller whose key no Secret holds.",
        headers={"authorization": "Bearer sk-wrong"},
        want=401,
    ),
]


# These use routed rather than serving, so a gateway that refuses every caller
# still passes them, and fails only the tests that need it to serve.
@pytest.mark.parametrize("case", REFUSED_CASES, ids=lambda case: case.name)
def test_refused(case: Case, routed: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """The InferenceGateway refuses a chat completion without a valid key."""
    r = control_plane_client.request(
        f"{routed.openai}/chat/completions",
        case.headers,
        {"model": routed.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == case.want, case.reason
