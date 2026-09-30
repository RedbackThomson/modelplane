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

import typing
import unittest

import yaml
from function import collector, stacks
from models.ai.modelplane.metricmapping import v1alpha1 as mmv1alpha1
from models.ai.modelplane.telemetrydestination import v1alpha1 as tdv1alpha1
from pydantic import ValidationError

_EXTENSIONS = {"oidc/acme": {"issuer_url": "https://issuer.acme.example"}}


def _sink(name: str = "primary", type_: str = "otlphttp", secret: str | None = None) -> tdv1alpha1.Sink:
    return tdv1alpha1.Sink.model_validate(
        {
            "name": name,
            "type": type_,
            "endpoint": "https://otel.acme.example",
            **({"secretRef": {"name": secret}, "auth": {"bearerTokenKey": "token"}} if secret else {}),
        }
    )


_SINKS = [_sink()]


def _metric_statements() -> list[str]:
    return collector.statements(list(stacks.BUILTIN_MAPPINGS))[1]


def _config(*, extensions: dict | None = None) -> dict:
    return yaml.safe_load(
        collector.config(
            "prod-us-east",
            list(stacks.BUILTIN_MAPPINGS),
            _SINKS,
            _EXTENSIONS if extensions is None else extensions,
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
        """A series the statements didn't rename is dropped."""
        self.assertIn("filter/modelplane", _config()["processors"])
        self.assertIn("filter/modelplane", _config()["service"]["pipelines"]["metrics"]["processors"])

    def test_cluster_is_stamped_here(self) -> None:
        """One receiver downstream sees a merged stream and can't tell senders apart."""
        attrs = _config()["processors"]["resource/cluster"]["attributes"]
        self.assertEqual(attrs, [{"key": "cluster", "value": "prod-us-east", "action": "upsert"}])

    def test_the_jobs_cover_disjoint_pods(self) -> None:
        """A pod two jobs both collect arrives twice, under two job names."""
        jobs = {
            j["job_name"]: j["relabel_configs"]
            for j in _config()["receivers"]["prometheus"]["config"]["scrape_configs"]
        }
        substrate = jobs["modelplane-substrate"]

        def predicate(rules: list[dict], action: str) -> set[tuple]:
            return {(tuple(r["source_labels"]), r["regex"]) for r in rules if r.get("action") == action}

        # Everything another job keeps, the substrate job drops on the same terms.
        for job in ("modelplane-engines", "modelplane-gateway", "modelplane-gpu"):
            for kept in predicate(jobs[job], "keep"):
                if kept[0] == ("__meta_kubernetes_pod_container_port_name",):
                    continue  # a port filter, not a pod filter
                self.assertIn(kept, predicate(substrate, "drop"), f"{job} keeps {kept}, substrate does not drop it")

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
        self.assertEqual(_config()["service"]["extensions"], ["oidc/acme"])
        self.assertNotIn("extensions", _config(extensions={})["service"])

    def test_energy_is_scaled_before_it_is_renamed(self) -> None:
        """DCGM counts millijoules, and the name says joules.

        The scale is a block ahead of the renames, not a line ahead. The
        processor finishes a block over every datapoint before the next one
        starts, so a rename sharing the block would strand every datapoint
        after the first at millijoules.
        """
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        self.assertEqual([b["context"] for b in blocks], ["datapoint", "metric"])
        self.assertTrue(any("value_double / 1000" in st for st in blocks[0]["statements"]))
        self.assertTrue(any("modelplane_energy_joules_total" in st for st in blocks[1]["statements"]))

    def test_dcgm_units_are_converted_to_the_unit_the_name_claims(self) -> None:
        """DCGM reports mJ and MiB; the names say joules and bytes."""
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        scales = " ".join(blocks[0]["statements"])
        self.assertIn("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", scales)
        self.assertIn("DCGM_FI_DEV_FB_USED", scales)

    def test_every_unit_the_api_offers_has_a_conversion(self) -> None:
        """A unit the API accepts with no conversion here is a KeyError at render time."""
        annotation = mmv1alpha1.Metric.model_fields["fromUnit"].annotation
        literal = next(a for a in typing.get_args(annotation) if typing.get_origin(a) is typing.Literal)
        self.assertEqual(set(typing.get_args(literal)), set(collector._UNIT_CONVERSION))

    def test_a_metric_name_cannot_end_the_comparison_early(self) -> None:
        """A quote in `from` would rename whatever the rest of the line matched."""
        with self.assertRaises(ValidationError):
            mmv1alpha1.Metric.model_validate({"from": 'x" or true or name == "y', "to": "modelplane_x"})
        for mapping in stacks.BUILTIN_MAPPINGS:
            for m in mapping.spec.metrics:
                round_tripped = mmv1alpha1.Metric.model_validate({"from": m.from_, "to": m.to})
                self.assertEqual(round_tripped.from_, m.from_)

    def test_a_value_rewrite_never_lands_in_the_metric_context(self) -> None:
        """value_double is a datapoint path; the collector refuses to start on it here."""
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        metric_block = next(b for b in blocks if b["context"] == "metric")
        self.assertFalse([st for st in metric_block["statements"] if "value_double" in st])
        for block in blocks:
            for st in block["statements"]:
                self.assertNotIn("set(name,", st)
                self.assertNotIn("set(value_double,", st)

    def test_sglang_latency_histograms_are_not_renamed(self) -> None:
        """Their buckets resolve to 100ms where vLLM's resolve to 1ms."""
        joined = " ".join(_metric_statements())
        self.assertNotIn("sglang:time_to_first_token_seconds", joined)
        self.assertNotIn("sglang:inter_token_latency", joined)


class TestObjects(unittest.TestCase):
    """The manifests this composes."""

    def _objects(self, secret: str | None = None) -> dict:
        sinks = [_sink(secret=secret)] if secret else _SINKS
        return {
            k: m for k, m, _ in collector.objects("prod-us-east", list(stacks.BUILTIN_MAPPINGS), sinks, _EXTENSIONS)
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
        self.assertIn("credentials-primary", [v["name"] for v in pod["volumes"]])
        self.assertEqual(pod["containers"][0]["envFrom"], [{"secretRef": {"name": "telemetry-credentials"}}])

    def test_each_sink_gets_its_own_credential_directory(self) -> None:
        """Two sinks can both hold a key called token, and neither reads the other's."""
        sinks = [
            _sink(name="vendor", secret="vendor-token"),
            _sink(name="prometheus", type_="prometheusremotewrite", secret="prom-token"),
        ]
        pod = {
            k: m for k, m, _ in collector.objects("prod-us-east", list(stacks.BUILTIN_MAPPINGS), sinks, _EXTENSIONS)
        }["collector"]["spec"]["template"]["spec"]
        mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["credentials-vendor"], "/etc/modelplane/telemetry/vendor")
        self.assertEqual(mounts["credentials-prometheus"], "/etc/modelplane/telemetry/prometheus")

    def test_two_sinks_of_one_type_do_not_collide(self) -> None:
        """The collector names a second instance of a component <type>/<name>."""
        rendered = collector.exporters([_sink(name="a"), _sink(name="b")])
        self.assertEqual(sorted(rendered), ["otlphttp/a", "otlphttp/b"])

    def test_a_sink_that_addresses_its_destination_another_way(self) -> None:
        """Kafka takes brokers, the debug exporter nothing; neither has an endpoint."""
        sinks = [
            tdv1alpha1.Sink.model_validate(
                {"name": "bus", "type": "kafka", "config": {"brokers": ["kafka.acme.example:9092"]}}
            ),
            tdv1alpha1.Sink.model_validate({"name": "seen", "type": "debug"}),
        ]
        rendered = collector.exporters(sinks)
        self.assertNotIn("endpoint", rendered["kafka/bus"])
        self.assertEqual(rendered["kafka/bus"]["brokers"], ["kafka.acme.example:9092"])
        self.assertEqual(rendered["debug/seen"], {})

    def test_auth_composes_its_own_authenticator(self) -> None:
        """The collector carries no credential on an exporter, only a reference."""
        sink = _sink(secret="telemetry-credentials")
        self.assertEqual(
            collector.authenticators([sink]),
            {"bearertokenauth/primary": {"filename": "/etc/modelplane/telemetry/primary/token"}},
        )
        self.assertEqual(
            collector.exporters([sink])["otlphttp/primary"]["auth"],
            {"authenticator": "bearertokenauth/primary"},
        )

    def test_a_sinks_own_config_cannot_redirect_it(self) -> None:
        """The endpoint is Modelplane's, and goes on after the operator's config."""
        sink = tdv1alpha1.Sink.model_validate(
            {
                "name": "primary",
                "type": "otlphttp",
                "endpoint": "https://otel.acme.example",
                "config": {"endpoint": "https://elsewhere.example", "compression": "gzip"},
            }
        )
        rendered = collector.exporters([sink])["otlphttp/primary"]
        self.assertEqual(rendered["endpoint"], "https://otel.acme.example")
        self.assertEqual(rendered["compression"], "gzip")

    def test_no_secret_mounts_nothing(self) -> None:
        pod = self._objects()["collector"]["spec"]["template"]["spec"]
        self.assertEqual([v["name"] for v in pod["volumes"]], ["config"])
        self.assertNotIn("envFrom", pod["containers"][0])

    def test_rbac_is_read_only(self) -> None:
        """Service discovery needs to list pods, and nothing needs to write."""
        rules = self._objects()["collector-clusterrole"]["rules"]
        verbs = {v for r in rules for v in r["verbs"]}
        self.assertEqual(verbs, {"get", "list", "watch"})
