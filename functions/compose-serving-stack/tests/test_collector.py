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

import re
import typing
import unittest

import yaml
from function import collector, stacks
from models.ai.modelplane.metricmapping import v1alpha1 as mmv1alpha1
from models.ai.modelplane.telemetrydestination import v1alpha1 as tdv1alpha1
from pydantic import ValidationError

# A client authenticator: an exporter needs one of those, not the oidc
# extension, which authenticates callers of a receiver.
_EXTENSIONS = {"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}}


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
    """The rename statements, which are the last of the four blocks."""
    return collector.statements(list(stacks.BUILTIN_MAPPINGS))[-1]


def _config(*, extensions: dict | None = None, sinks: list | None = None) -> dict:
    return yaml.safe_load(
        collector.config(
            "prod-us-east",
            list(stacks.BUILTIN_MAPPINGS),
            _SINKS if sinks is None else sinks,
            _EXTENSIONS if extensions is None else extensions,
        )
    )


class TestConfig(unittest.TestCase):
    """The collector configuration this renders."""

    def test_pipeline_order(self) -> None:
        """The rename runs before the identity is lifted onto the resource.

        Discovery writes the identity onto each datapoint and groupbyattrs
        lifts it; a statement matching on a metric's name has to run while the
        datapoints are still where the rename can reach them.
        """
        procs = _config()["service"]["pipelines"]["metrics"]["processors"]
        self.assertLess(procs.index("transform/modelplane"), procs.index("groupbyattrs/identity"))
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

    def test_only_the_identity_survives_to_the_exporter(self) -> None:
        """Discovery attaches the pod's name and uid; neither is the deployment's."""
        blocks = _config()["processors"]["transform/identity"]["metric_statements"]
        statement = blocks[0]["statements"][0]
        # OTTL quotes with double quotes. A Python list renders single ones and
        # the collector refuses to start, which a unit test on shape won't catch.
        self.assertNotIn("'", statement)
        self.assertIn('keep_keys(resource.attributes, ["cluster"', statement)
        pipeline = _config()["service"]["pipelines"]["metrics"]["processors"]
        self.assertLess(pipeline.index("transform/identity"), pipeline.index("groupbyattrs/identity"))

    def test_the_identity_is_lifted_onto_the_resource(self) -> None:
        """Without this a series arrives carrying only the cluster.

        Discovery writes the identity onto each datapoint. An exporter that
        flattens a series into labels reads the resource, so something has to
        move it, and this is the only processor that does. Removing it as a
        no-op strips every series of what says who it belongs to - verified on
        a cluster, where the resource came back carrying `cluster` alone.
        """
        cfg = _config()
        self.assertEqual(cfg["processors"]["groupbyattrs/identity"]["keys"], list(collector._IDENTITY))
        self.assertIn("groupbyattrs/identity", cfg["service"]["pipelines"]["metrics"]["processors"])

    def test_a_part_is_extracted_before_anything_selects_on_it(self) -> None:
        """A label or a unit for an extracted part names a metric that must exist.

        The extraction mints `<name>_count`, and a datapoint statement for it
        selects on that name. Run the datapoint block first and it matches
        nothing, silently.
        """
        mapping = mmv1alpha1.MetricMapping.model_validate(
            {
                "spec": {
                    "metrics": [
                        {
                            "from": "my_engine_duration_ms",
                            "to": "modelplane_requests_total",
                            "part": "Count",
                            "fromUnit": "Milliseconds",
                            "labels": [{"name": "status", "value": "ok"}],
                        }
                    ]
                }
            }
        )
        blocks = collector._transform([mapping])["metric_statements"]
        contexts = [b["context"] for b in blocks]
        self.assertEqual(contexts, ["metric", "metric", "datapoint", "metric"])
        self.assertIn("extract_count_metric", blocks[0]["statements"][0])
        # Everything selecting on the extracted name comes after the extraction.
        for block in blocks[1:]:
            for statement in block["statements"]:
                self.assertIn("my_engine_duration_ms_count", statement)

    def test_every_job_carries_something_unique_to_its_target(self) -> None:
        """Two producers whose series are identical are one series, and one is lost.

        The modelplane identity names an engine and nothing else: a gateway pod
        carries none of it, two replicas of a substrate controller share a
        namespace, and a ModelReplica with copies > 1 runs several pods under
        one replica index.
        """
        self.assertIn("service.instance.id", collector._IDENTITY)
        statement = _config()["processors"]["transform/identity"]["metric_statements"][0]["statements"][0]
        self.assertIn('"service.instance.id"', statement)

    def test_a_scrape_spike_cannot_take_the_collector_down(self) -> None:
        """Nothing bounds what one interval brings off a fleet of engines."""
        cfg = _config()
        self.assertIn("memory_limiter", cfg["processors"])
        self.assertEqual(cfg["service"]["pipelines"]["metrics"]["processors"][0], "memory_limiter")

    def test_the_port_rewrite_matches_an_ipv6_pod(self) -> None:
        """__address__ is [2001:db8::1]:9090 there, which [^:]+ never matches."""
        rule = next(
            r
            for j in _config()["receivers"]["prometheus"]["config"]["scrape_configs"]
            if j["job_name"] == "modelplane-substrate"
            for r in j["relabel_configs"]
            if r.get("target_label") == "__address__"
        )
        for address in ("10.1.0.5:8000", "[2001:db8::1]:9090"):
            matched = re.fullmatch(rule["regex"], f"{address};9402")
            assert matched is not None, address
            self.assertTrue(matched.expand(r"\1:\2").endswith(":9402"))

    def test_engine_scrape_selects_the_port_by_name(self) -> None:
        """Matching by number would find the pd-sidecar on a disaggregated pod."""
        jobs = {j["job_name"]: j for j in _config()["receivers"]["prometheus"]["config"]["scrape_configs"]}
        keeps = [r for r in jobs["modelplane-engines"]["relabel_configs"] if r.get("action") == "keep"]
        self.assertIn("__meta_kubernetes_pod_container_port_name", [k["source_labels"][0] for k in keeps])

    def test_gateway_has_a_target_of_its_own(self) -> None:
        """Its GenAI metrics are on the ext-proc sidecar, not the proxy's port."""
        jobs = [j["job_name"] for j in _config()["receivers"]["prometheus"]["config"]["scrape_configs"]]
        self.assertIn("modelplane-gateway", jobs)

    def test_both_spellings_of_remote_write_keep_their_identity(self) -> None:
        """The exporter registers as prometheus_remote_write in 0.161.0.

        prometheusremotewrite is the older name it still answers to. A sink
        writing the one the collector's own documentation gives would
        otherwise match no default here and export every series stripped of
        the cluster, deployment, engine and role it belongs to - silently,
        because the sink itself works.
        """
        for type_ in ("prometheus_remote_write", "prometheusremotewrite", "prometheus"):
            with self.subTest(type=type_):
                exporters = _config(sinks=[_sink(type_=type_)])["exporters"]
                exporter = next(v for k, v in exporters.items() if k.startswith(f"{type_}/"))
                self.assertTrue(exporter["resource_to_telemetry_conversion"]["enabled"])

    def test_extensions_are_declared_to_the_service(self) -> None:
        """An authenticator the service doesn't list is one the collector won't load."""
        self.assertEqual(_config()["service"]["extensions"], ["oauth2client/acme"])
        self.assertNotIn("extensions", _config(extensions={})["service"])

    def test_energy_is_scaled_before_it_is_renamed(self) -> None:
        """DCGM counts millijoules, and the name says joules.

        The scale is a block ahead of the renames, not a line ahead. The
        processor finishes a block over every metric before the next one
        starts, so a rename sharing the block would strand every metric
        after the first at millijoules.
        """
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        self.assertEqual([b["context"] for b in blocks], ["metric", "metric"])
        self.assertTrue(any("scale_metric(0.001)" in st for st in blocks[0]["statements"]))
        self.assertTrue(any("modelplane_energy_joules_total" in st for st in blocks[-1]["statements"]))

    def test_a_conversion_reaches_a_histogram_bucket(self) -> None:
        """Setting value_double converts a gauge and leaves a histogram lying.

        A histogram holds its measurements in its sum, its minimum and maximum
        and every bucket boundary, none of which is value_double. Renaming one
        to seconds with its buckets still at milliseconds puts every quantile
        a thousand times out, and nothing says so.
        """
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        for block in blocks:
            for statement in block["statements"]:
                self.assertNotIn("value_double", statement)
        scales = next(b for b in blocks if any("scale_metric" in st for st in b["statements"]))
        self.assertEqual(scales["context"], "metric")

    def test_every_conversion_factor_is_a_float_literal(self) -> None:
        """scale_metric takes a float, and 1048576 is an integer to OTTL.

        The collector refuses to start on it - "must be a float" - which
        takes the whole cluster's telemetry down, and nothing short of
        running the collector catches it.
        """
        for unit, factor in collector._UNIT_FACTOR.items():
            with self.subTest(unit=unit):
                self.assertIn(".", factor, "an OTTL float literal needs a decimal point")
                float(factor)

    def test_a_conversion_cannot_drop_the_batch_it_rides_in(self) -> None:
        """scale_metric refuses an exponential histogram.

        Under the default error mode that one refusal fails the whole batch:
        every metric from every pod in the scrape is lost, not the one it
        could not convert.
        """
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        scales = next(b for b in blocks if any("scale_metric" in st for st in b["statements"]))
        self.assertEqual(scales["error_mode"], "ignore")

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
        self.assertEqual(set(typing.get_args(literal)), set(collector._UNIT_FACTOR))

    def test_a_percentage_is_divided_into_a_ratio(self) -> None:
        """A component counting 0 to 100 under a name that says a ratio is 100x out.

        vLLM and SGLang both publish a fraction, so no built-in needs this, but
        vLLM's is called kv_cache_usage_perc - the name is no guide, and an
        engine that means it has to be able to say so.
        """
        mapping = mmv1alpha1.MetricMapping.model_validate(
            {
                "spec": {
                    "metrics": [
                        {
                            "from": "my_engine_cache_percent",
                            "to": "modelplane_kv_cache_utilization_ratio",
                            "fromUnit": "Percent",
                        }
                    ]
                }
            }
        )
        _, scale, _, _ = collector.statements([mapping])
        self.assertEqual(scale, ['scale_metric(0.01) where metric.name == "my_engine_cache_percent"'])

    def test_a_metric_name_cannot_end_the_comparison_early(self) -> None:
        """A quote in `from` would rename whatever the rest of the line matched."""
        with self.assertRaises(ValidationError):
            mmv1alpha1.Metric.model_validate({"from": 'x" or true or name == "y', "to": "modelplane_x"})
        for mapping in stacks.BUILTIN_MAPPINGS:
            for m in mapping.spec.metrics:
                round_tripped = mmv1alpha1.Metric.model_validate({"from": m.from_, "to": m.to})
                self.assertEqual(round_tripped.from_, m.from_)

    def test_a_label_value_cannot_end_the_string_it_sits_in(self) -> None:
        """`from` is pattern-constrained; a label's value cannot be.

        A value and a `values` remap carry whatever vocabulary the component
        already writes, so the schema has to take free text. A quote in one
        would close the OTTL literal early and leave the remainder of the
        value as OTTL - at best the collector refuses to start.
        """
        mapping = mmv1alpha1.MetricMapping.model_validate(
            {
                "spec": {
                    "metrics": [
                        {
                            "from": "my_engine_finish",
                            "to": "modelplane_requests_total",
                            "labels": [{"name": "reason", "from": "finish", "values": {'ab"c': 'x"y'}}],
                        }
                    ]
                }
            }
        )
        _, _, datapoint, _ = collector.statements([mapping])
        joined = " ".join(datapoint)
        self.assertIn(r'"ab\"c"', joined)
        self.assertIn(r'"x\"y"', joined)

    def test_carrying_a_label_onto_itself_keeps_it(self) -> None:
        """`from` equal to `name` is how a mapping remaps values in place.

        The delete that stops a carried label costing twice the cardinality
        would otherwise take the label the statements before it just set, and
        the series would lose the label entirely.
        """
        mapping = mmv1alpha1.MetricMapping.model_validate(
            {
                "spec": {
                    "metrics": [
                        {
                            "from": "my_engine_finish",
                            "to": "modelplane_requests_total",
                            "labels": [{"name": "reason", "from": "reason", "values": {"eos": "stop"}}],
                        }
                    ]
                }
            }
        )
        _, _, datapoint, _ = collector.statements([mapping])
        self.assertFalse([st for st in datapoint if st.startswith("delete_key")])

    def test_a_value_rewrite_never_lands_in_the_metric_context(self) -> None:
        """value_double is a datapoint path; the collector refuses to start on it here."""
        blocks = _config()["processors"]["transform/modelplane"]["metric_statements"]
        metric_block = next(b for b in blocks if b["context"] == "metric")
        self.assertFalse([st for st in metric_block["statements"] if "value_double" in st])
        for block in blocks:
            for st in block["statements"]:
                self.assertNotIn("set(name,", st)
                self.assertNotIn("set(value_double,", st)

    def test_sglang_carries_no_queue_time_or_preemption(self) -> None:
        """SGLang publishes neither, so there is nothing to rename onto them.

        Checked against a running SGLang v0.4.9.post2: it has no per-request
        queue-time metric and no retraction counters at all. The nearest
        thing, sglang:avg_request_queue_latency, is a gauge of the mean over
        the last batch - a different measurement from vLLM's per-request
        histogram, and one name holding both makes a fleet quantile
        meaningless.
        """
        sglang = {
            m.from_: m.to
            for mapping in stacks.BUILTIN_MAPPINGS
            for m in mapping.spec.metrics
            if m.from_.startswith("sglang:")
        }
        self.assertTrue(sglang, "the SGLang built-in went missing")
        self.assertNotIn("modelplane_request_queue_seconds", sglang.values())
        self.assertNotIn("modelplane_requests_preempted_total", sglang.values())
        self.assertFalse([k for k in sglang if "retracted" in k or "queue_time" in k])

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
