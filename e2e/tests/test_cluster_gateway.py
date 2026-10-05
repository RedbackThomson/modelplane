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

"""Tests that the cluster gateway fronting the engines refuses callers without a client certificate.

The other tests that send requests go through the InferenceGateway, which holds
a certificate, so none of them would notice this lapsing. A ClientTrafficPolicy that stopped
applying, or an HTTP listener beside the HTTPS one, would leave the engines open
to anything that can reach the load balancer.
"""

import dataclasses

import pytest

from e2e import gateway


@dataclasses.dataclass
class Case:
    """A connection the cluster gateway refuses."""

    name: str
    reason: str
    scheme: str
    # The curl exit codes that mean the gateway refused the connection. Any
    # other code, such as an unresolved name, is a different failure.
    want: set[int]


REFUSED_CASES = [
    # 35: the TLS handshake failed. 52: an empty reply. 55 and 56: the
    # connection broke mid-handshake.
    Case(
        name="NoClientCertificate",
        reason="The cluster gateway refuses a caller that presents no client certificate, during the handshake.",
        scheme="https",
        want={35, 52, 55, 56},
    ),
    # The serving HTTPRoutes carry no sectionName, so they attach to every
    # listener there is, and an HTTP listener would serve the engines without a
    # certificate. The gateway's only listener is HTTPS, and the load balancer
    # publishes a port per listener. 7: the connection was refused. 28: it timed
    # out. 52 and 56: something answered port 80 without serving HTTP.
    Case(
        name="Plaintext",
        reason="The cluster gateway serves nothing over plain HTTP on port 80.",
        scheme="http",
        want={7, 28, 52, 56},
    ),
]


@pytest.mark.parametrize("case", REFUSED_CASES, ids=lambda case: case.name)
def test_refused(case: Case, workload_client: gateway.Client, cluster_gateway: str) -> None:
    """The cluster gateway refuses a connection that carries no client certificate."""
    # The trailing dot skips the pod's search domains, which ndots:5 would
    # otherwise try first.
    assert workload_client.connect(f"{case.scheme}://{cluster_gateway}./v1/models") in case.want, case.reason
