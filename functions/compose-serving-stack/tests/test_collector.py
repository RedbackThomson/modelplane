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

"""Tests for the collector this stack composes."""

import unittest

import yaml
from function import collector, stacks

_EXPORTERS = {"otlphttp": {"endpoint": "https://otel.acme.example", "auth": {"authenticator": "bearertokenauth"}}}
_EXTENSIONS = {"bearertokenauth": {"filename": "/etc/modelplane/telemetry/token"}}


def _config(*, keep_raw: bool = False, extensions: dict | None = None) -> dict:
    return yaml.safe_load(
        collector.config(
            "prod-us-east",
            list(stacks.METRIC_STATEMENTS),
            _EXPORTERS,
            _EXTENSIONS if extensions is None else extensions,
            keep_raw=keep_raw,
        )
    )


class TestConfig(unittest.TestCase):
    """The collector configuration this renders."""

    def test_pipeline_order(self) -> None:
        """groupbyattrs runs before the merge, or the merge combines nothing.

        A pod's identity is a resource attribute, which a metric processor
        can't see, so the resources have to be stripped and merged first.
        """
        procs = _config()["service"]["pipelines"]["metrics"]["processors"]
        self.assertLess(procs.index("transform/modelplane"), procs.index("groupbyattrs/replicas"))
        self.assertEqual(procs[-1], "batch")

    def test_only_modelplane_leaves_the_cluster(self) -> None:
        """A series the statements didn't rename is dropped, unless asked for."""
        self.assertIn("filter/modelplane", _config()["processors"])
        self.assertNotIn("filter/modelplane", _config(keep_raw=True)["processors"])

    def test_cluster_is_stamped_here(self) -> None:
        """One receiver downstream sees a merged stream and can't tell senders apart."""
        attrs = _config()["processors"]["resource/cluster"]["attributes"]
        self.assertEqual(attrs, [{"key": "cluster", "value": "prod-us-east", "action": "upsert"}])

    def test_engine_scrape_selects_the_port_by_name(self) -> None:
        """Matching by number would find the pd-sidecar on a disaggregated pod."""
        jobs = {j["job_name"]: j for j in _config()["receivers"]["prometheus"]["config"]["scrape_configs"]}
        keeps = [r for r in jobs["modelplane-engines"]["relabel_configs"] if r.get("action") == "keep"]
        self.assertIn("__meta_kubernetes_pod_container_port_name", [k["source_labels"][0] for k in keeps])

    def test_gateway_has_a_target_of_its_own(self) -> None:
        """Its GenAI metrics are on the ext-proc sidecar, not the proxy's port."""
        jobs = [j["job_name"] for j in _config()["receivers"]["prometheus"]["config"]["scrape_configs"]]
        self.assertIn("modelplane-gateway", jobs)

    def test_extensions_are_declared_to_the_service(self) -> None:
        """An authenticator the service doesn't list is one the collector won't load."""
        self.assertEqual(_config()["service"]["extensions"], ["bearertokenauth"])
        self.assertNotIn("extensions", _config(extensions={})["service"])

    def test_energy_is_scaled_before_it_is_renamed(self) -> None:
        """DCGM counts millijoules, and the name says joules."""
        statements = stacks.METRIC_STATEMENTS
        scale = next(i for i, s in enumerate(statements) if "value_double / 1000" in s)
        rename = next(i for i, s in enumerate(statements) if "modelplane_energy_joules_total" in s)
        self.assertLess(scale, rename)

    def test_sglang_latency_histograms_are_not_renamed(self) -> None:
        """Their buckets resolve to 100ms where vLLM's resolve to 1ms."""
        joined = " ".join(stacks.METRIC_STATEMENTS)
        self.assertNotIn("sglang:time_to_first_token_seconds", joined)
        self.assertNotIn("sglang:inter_token_latency", joined)


class TestObjects(unittest.TestCase):
    """The manifests this composes."""

    def _objects(self, secret: str | None = None) -> dict:
        return {
            k: m
            for k, m, _ in collector.objects(
                "prod-us-east", list(stacks.METRIC_STATEMENTS), _EXPORTERS, _EXTENSIONS, secret, keep_raw=False
            )
        }

    def test_config_hash_is_stable_across_processes(self) -> None:
        """hash() is seeded per process, so it would redeploy on every reconcile."""
        first = self._objects()["collector"]["spec"]["template"]["metadata"]["annotations"]
        second = self._objects()["collector"]["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(first, second)
        self.assertRegex(first["modelplane.ai/config-hash"], r"^[0-9a-f]{16}$")

    def test_credentials_mount_as_a_file_and_an_environment_variable(self) -> None:
        """A rotated token in an environment variable needs a restart to be read."""
        pod = self._objects(secret="telemetry-credentials")["collector"]["spec"]["template"]["spec"]
        self.assertIn("credentials", [v["name"] for v in pod["volumes"]])
        self.assertEqual(pod["containers"][0]["envFrom"], [{"secretRef": {"name": "telemetry-credentials"}}])

    def test_no_secret_mounts_nothing(self) -> None:
        pod = self._objects()["collector"]["spec"]["template"]["spec"]
        self.assertEqual([v["name"] for v in pod["volumes"]], ["config"])
        self.assertNotIn("envFrom", pod["containers"][0])

    def test_rbac_is_read_only(self) -> None:
        """Service discovery needs to list pods, and nothing needs to write."""
        rules = self._objects()["collector-clusterrole"]["rules"]
        verbs = {v for r in rules for v in r["verbs"]}
        self.assertEqual(verbs, {"get", "list", "watch"})
