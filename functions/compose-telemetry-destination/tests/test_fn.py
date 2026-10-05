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
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.telemetrydestination import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-telemetry-destination."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _telemetry_destination(*, sinks: list[v1alpha1.Sink], extensions: dict[str, Any] | None) -> fnv1.Resource:
    """The TelemetryDestination XR named default, exporting through sinks, with extensions unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.TelemetryDestination(
                metadata=metav1.ObjectMeta(name="default"),
                spec=v1alpha1.Spec(sinks=sinks, extensions=extensions),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_telemetry_destination(*, status: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired TelemetryDestination XR, carrying status, or only its readiness if status is None."""
    if status is None:
        return fnv1.Resource(ready=ready)
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}), ready=ready)


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# A sink's credential requirement names modelplane-system: unqualified, it would
# resolve a Secret of that name in any namespace, and accept the wrong
# credential.
COMPOSE_CASES = [
    Case(
        name="AuthenticatorDefined",
        reason=(
            "With its sink's authenticator defined under extensions, the destination is Accepted and Ready, "
            "naming the sink it exports through."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="primary",
                            type="otlphttp",
                            endpoint="https://otel.acme.example",
                            config={"auth": {"authenticator": "oauth2client/acme"}},
                        ),
                    ],
                    extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status={}, ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through otlphttp/primary",
                ),
            ],
        ),
    ),
    Case(
        name="NoAuthenticator",
        reason="A sink that references no authenticator needs no extensions, so the destination is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="prom",
                            type="prometheusremotewrite",
                            endpoint="https://prom.acme.example/api/v1/write",
                        ),
                    ],
                    extensions=None,
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status={}, ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through prometheusremotewrite/prom",
                ),
            ],
        ),
    ),
    Case(
        name="BearerTokenAuth",
        reason=(
            "With a sink that sets auth and a secretRef, and names no authenticator in its config, "
            "the destination is Ready once its credential Secret is present."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="primary",
                            type="otlphttp",
                            endpoint="https://otel.acme.example",
                            secretRef=v1alpha1.SecretRef(name="telemetry-credentials"),
                            auth=v1alpha1.Auth(bearerTokenKey="token"),
                        ),
                    ],
                    extensions=None,
                ),
            ),
            required_resources={
                "secret-primary": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "telemetry-credentials"}}
                            ),
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status={}, ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "secret-primary": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="telemetry-credentials",
                        namespace="modelplane-system",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through otlphttp/primary",
                ),
            ],
        ),
    ),
    Case(
        name="SecretExists",
        reason="Once its sink's credential Secret exists, the destination is Accepted and Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="primary",
                            type="otlphttp",
                            endpoint="https://otel.acme.example",
                            config={"auth": {"authenticator": "oauth2client/acme"}},
                            secretRef=v1alpha1.SecretRef(name="telemetry-credentials"),
                        ),
                    ],
                    extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
                ),
            ),
            required_resources={
                "secret-primary": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "telemetry-credentials"}}
                            ),
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status={}, ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "secret-primary": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="telemetry-credentials",
                        namespace="modelplane-system",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                    message="Exporting through otlphttp/primary",
                ),
            ],
        ),
    ),
    Case(
        name="SecretUnresolved",
        reason="Until its sink's credential Secret requirement resolves, the destination waits for it and isn't Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="primary",
                            type="otlphttp",
                            endpoint="https://otel.acme.example",
                            config={"auth": {"authenticator": "oauth2client/acme"}},
                            secretRef=v1alpha1.SecretRef(name="telemetry-credentials"),
                        ),
                    ],
                    extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status=None, ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "secret-primary": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="telemetry-credentials",
                        namespace="modelplane-system",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForSecret",
                    message="Waiting for the credential Secret to resolve",
                ),
            ],
        ),
    ),
    Case(
        name="UnknownAuthenticator",
        reason="A sink naming an authenticator no extension defines leaves the destination not Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="primary",
                            type="otlphttp",
                            endpoint="https://otel.acme.example",
                            config={"auth": {"authenticator": "oauth2client/acme"}},
                        ),
                    ],
                    extensions=None,
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status=None, ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="UnknownAuthenticator",
                    message="No extension defines oauth2client/acme, so the collector would refuse to start",
                ),
            ],
        ),
    ),
    Case(
        name="SecretMissing",
        reason="When its sink's credential Secret doesn't exist, the destination isn't Ready and names the Secret.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_telemetry_destination(
                    sinks=[
                        v1alpha1.Sink(
                            name="primary",
                            type="otlphttp",
                            endpoint="https://otel.acme.example",
                            config={"auth": {"authenticator": "oauth2client/acme"}},
                            secretRef=v1alpha1.SecretRef(name="telemetry-credentials"),
                        ),
                    ],
                    extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
                ),
            ),
            required_resources={"secret-primary": fnv1.Resources()},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_telemetry_destination(status=None, ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "secret-primary": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="telemetry-credentials",
                        namespace="modelplane-system",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="SecretNotFound",
                    message="Secret telemetry-credentials does not exist, so sink primary has no credential to send with",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction reports whether the destination can be sent through."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
