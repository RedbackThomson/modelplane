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

"""Tests that the InferenceGateway routes requests to ModelService ml-team/mock."""

from e2e import gateway


def test_openai(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """An OpenAI chat completion naming the ModelService returns 200."""
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        gateway.BEARER,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200


def test_anthropic(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """An Anthropic Messages API request naming the ModelService returns 200.

    The endpoint's API is OpenAI, so the gateway translates the request. The
    mock serves /v1/messages too, the way vLLM does, so a 200 alone doesn't
    tell translation from passthrough. Anthropic clients send the key in
    x-api-key.
    """
    r = control_plane_client.request(
        f"{serving.anthropic}/messages",
        {"x-api-key": gateway.CALLER_KEY, "anthropic-version": "2023-06-01"},
        {"model": serving.model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200


def test_served_model(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """The response names the model the engine serves, not the ModelService the caller asked for.

    The engine answers only to the name Modelplane started it under, and refuses
    anything else with a 404. So a 200 already shows the gateway rewrote the
    caller's ModelService name to the deployment's. This asserts the visible
    half of the same mechanism.
    """
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        gateway.BEARER,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200
    assert r.json()["model"] == "ml-team/mock-demo"


def test_unclaimed(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """A model no ModelService claims routes nowhere.

    This catches a route that matches too broadly, which would send a caller to
    an arbitrary backend. A 404 only means that once the claimed name serves, so
    this waits for it.
    """
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        gateway.BEARER,
        {"model": "ml-team/nope", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 404


def test_models(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """/v1/models lists the ModelService.

    It lists only models a route matches exactly, so this also shows the route
    matches the name exactly rather than by pattern.
    """
    r = control_plane_client.request(f"{serving.openai}/models", gateway.BEARER)
    assert r.status == 200
    assert serving.model in [m["id"] for m in r.json()["data"]]
