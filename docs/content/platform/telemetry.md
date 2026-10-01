---
title: Monitor the Fleet
weight: 37
aliases:
- /guides/collecting-engine-metrics/
- /guides/telemetry/
description: Collect normalized metrics across the fleet and send them anywhere that speaks OTLP.
---
<!-- vale write-good.Passive = NO -->

Modelplane runs an OpenTelemetry collector on every inference cluster. It collects from
every component Modelplane installs, which is more than your engines. It renames each
component's series to a single `modelplane_*` vocabulary and pushes to a collector on your
control plane. That collector is your fleet's
single egress point, and it sends to any backend that speaks OTLP.

Modelplane has no API for this: nothing to write, and nothing to keep in sync as your
deployments change.

## What you get

Every series carries `cluster`, and `job` and `instance` naming the target it was scraped
from. A series about a deployment also carries `deployment`, `replica`, `namespace`,
`engine`, and `role`.

Each replica publishes its own series. Combine them in the query, the way the metric's
`acrossReplicas` says: `sum by (deployment)` for anything counted, `avg by (deployment)`
for a ratio, `max by (deployment)` for a saturation figure an alert fires on. The
collector doesn't add them up for you, because a scrape of one replica is one batch, and
adding readings taken at different moments is not the traffic that happened.

The replica is an index rather than a pod, so it is bounded by the replica count and
survives a restart and a rolling update. Group by it, not by `instance`.

`instance` is the pod's address, and it is there because two pods writing one series is one
series with one of them lost - a deployment running several pods per replica, or two
gateway pods, have nothing else to tell them apart. It does turn over on a rolling update,
so a query that groups by it grows a series every time you deploy. Aggregate it away.

Some of what you can read:

| Metric | Means |
| --- | --- |
| `modelplane_frontend_ttft_seconds` | Time to the first token, measured at the gateway |
| `modelplane_frontend_tpot_seconds` | Time per output token, measured at the gateway |
| `modelplane_frontend_request_duration_seconds` | What the caller waited, end to end |
| `modelplane_request_queue_seconds` | How long a request waited before the engine started |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization_ratio` | KV-cache occupancy, averaged over replicas |
| `modelplane_request_input_tokens` | Prompt size, as a histogram |
| `modelplane_request_output_tokens` | Generated length, as a histogram |
| `modelplane_gpu_memory_used_bytes` | Framebuffer memory in use, per GPU |
| `modelplane_energy_joules_total` | Energy drawn since the driver last reloaded |

Latency appears twice on purpose. The `frontend_` series are what your caller experienced,
measured at the gateway. The engine's own series are what the engine spent. When the
frontend number is slow and the engine number isn't, the problem is routing, queueing, or
the network rather than the model.

Saturation gauges come as a pair. The average is what you plan capacity against; the `_max`
is what you alert on, because three replicas at 0.3 and one at 0.99 average to something
comfortable while the fourth evicts and recomputes. A high `_max` beside
`modelplane_requests_preempted_total` climbing is one replica thrashing.

<!-- vale Google.Acronyms = NO -->
No series names a pod. Replicas are interchangeable, so they're summed before the metrics
leave the cluster; a rolling update would otherwise leave a dead series behind for every pod
it replaced.
<!-- vale Google.Acronyms = YES -->

## Sending it somewhere

Create a `TelemetryDestination` naming whatever you already run:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  sinks:
  - name: primary
    type: otlphttp
    endpoint: https://otel.example.internal
```

`type` names a collector exporter, by the name OpenTelemetry gives it.

Put the credential in a Secret, name it with the sink's `secretRef`, and say which key holds
the token. Modelplane composes the authenticator and wires it up, and the token never
appears in `kubectl get -o yaml`:

```yaml
spec:
  sinks:
  - name: primary
    type: otlphttp
    endpoint: https://otel.example.internal
    secretRef:
      name: telemetry-credentials
    auth:
      bearerTokenKey: token
```

It reads the token from a file rather than the environment, so rotating it doesn't need the
collector restarted.

If you run Prometheus, export to that instead and query the fleet there:

```yaml
spec:
  sinks:
  - name: prometheus
    type: prometheusremotewrite
    endpoint: https://prom.example.internal/api/v1/write
```

Name more than one sink and every one gets the whole stream. Each carries its own
credential, so a vendor and your own Prometheus don't have to share a Secret:

```yaml
spec:
  sinks:
  - name: vendor
    type: otlphttp
    endpoint: https://otel.vendor.example
    secretRef:
      name: vendor-token
    auth:
      bearerTokenKey: token
  - name: prometheus
    type: prometheusremotewrite
    endpoint: https://prom.example.internal/api/v1/write
```

That is two copies of the fleet's metrics, billed twice.

Anything else the exporter takes goes under `config`, passed through as you wrote it:

```yaml
  - name: vendor
    type: otlphttp
    endpoint: https://otel.vendor.example
    config:
      compression: gzip
      sending_queue:
        queue_size: 10000
      tls:
        ca_file: /etc/ssl/certs/internal.pem
```

Modelplane doesn't model what an exporter is, so its TLS, retry and queue settings all work,
and a sink keeps working when the collector gains a setting Modelplane has never heard of.
An authentication scheme Modelplane doesn't compose works the same way: define the extension
under `spec.extensions` and name it from the sink's `config`, which is what the `auth` block
above does for you.

Until you create one, Modelplane composes no collectors: nothing here stores anything, so
collecting with nowhere to send it would spend GPU-cluster memory on samples nobody reads.
Creating a destination turns collection on everywhere at once, and there's no per-deployment
opt-out.

Your clusters reach the control plane, and only the control plane reaches your backend. A
cluster with no route to your observability stack still reports, and the backend's
credential lives in one place instead of on every GPU cluster.

## Computing rates, quantiles, and ratios

A collector transforms each measurement as it passes it on. It holds no history, so it
produces no rates and no quantiles. Your backend does that. A fleet-wide p99:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_frontend_ttft_seconds_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

Modelplane has no dashboards of its own. What it exports is counters and histogram buckets, and
your backend derives the rates and quantiles at query time. To precompute them instead,
export to Prometheus and write recording rules there.

## Engines

Modelplane renames vLLM's and SGLang's own metrics for you, so neither needs a mapping.
SGLang needs one flag to publish them at all, below.

Any other OpenAI-compatible engine reports its top-line numbers with no configuration. The
gateway measures those, not the engine, so `modelplane_frontend_*` works for an engine
Modelplane has never seen.

To normalize that engine's own metrics as well, create a `MetricMapping`:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  metrics:
  - from: my_engine_queued_requests
    to: modelplane_requests_waiting
  - from: my_engine_kv_transfer_ms
    fromUnit: Milliseconds
    to: modelplane_request_kv_transfer_seconds
```

Modelplane renders every mapping into every cluster's collector, so you write one once.
`from` is the name your engine emits and `to` is what Modelplane calls it.

Say `fromUnit` whenever the engine measures in something other than the unit the name
claims, and Modelplane converts to the base one. Skipping it is the expensive mistake here:
a series named `_seconds` that holds milliseconds reads a thousand times fast, and nothing
downstream can tell.

Rename only where the measurements agree. Two engines' histograms under one name are worth
less than nothing if their buckets disagree, because a quantile over them is wrong rather
than approximate.

SGLang publishes `/metrics` only when it runs with `--enable-metrics`, so add that to its
engine args. vLLM needs nothing.

## Why engine latency and gateway latency differ

`modelplane_request_ttft_seconds` comes from the engine, and engines bucket their
histograms differently. vLLM resolves down to a millisecond. SGLang resolves to a hundred
of them. A quantile across both is wrong, not approximate. Use the engine series to compare
one engine against itself, and the `frontend_` series for anything fleet-wide.

Some measurements don't translate at all. SGLang's inter-token latency isn't vLLM's time per
output token, so neither is renamed onto a shared name. The gateway measures time per output
token for both.

## Migrating from a hand-written `PodMonitor`

Modelplane used to have you write a `PodMonitor` and reach an in-cluster Prometheus over a
`port-forward`. Both are gone. Three steps to move across, and two of them fail quietly if
you skip them.

**Keep your Prometheus, and point a destination at it.** Collection becomes a push, so your
store stops scraping and starts receiving. Same Prometheus, same retention, same Grafana:

```yaml
spec:
  sinks:
  - name: prometheus
    type: prometheusremotewrite
    config:
      endpoint: http://prometheus.monitoring.svc:9090/api/v1/write
```

**Delete the monitors you wrote.** A `PodMonitor` or `ScrapeConfig` pointed at your engines
keeps working against your own Prometheus, so nothing appears to break and you collect
everything twice, under `vllm:*` and under `modelplane_*`, paying for both. One written
against Modelplane's Prometheus stops being read by anything, because the operator goes with
the stack.

**Rewrite your dashboard queries.** Names change, and so do the labels: group by
`deployment` rather than `model_name`, and every series carries `cluster`, `replica`, and
the `instance` it was scraped from. A panel that showed one engine now shows one pod, so
wrap it in `sum by (deployment)` or the aggregation that metric's `acrossReplicas` names.

| Was | Is |
| --- | --- |
| `vllm:time_to_first_token_seconds` | `modelplane_request_ttft_seconds` |
| `vllm:e2e_request_latency_seconds` | `modelplane_request_duration_seconds` |
| `vllm:request_queue_time_seconds` | `modelplane_request_queue_seconds` |
| `vllm:request_prefill_time_seconds` | `modelplane_request_prefill_seconds` |
| `vllm:request_decode_time_seconds` | `modelplane_request_decode_seconds` |
| `vllm:num_requests_running` | `modelplane_requests_running` |
| `vllm:num_requests_waiting` | `modelplane_requests_waiting` |
| `vllm:kv_cache_usage_perc` | `modelplane_kv_cache_utilization_ratio` |
| `vllm:num_preemptions_total` | `modelplane_requests_preempted_total` |
| `vllm:prefix_cache_hits_total` | `modelplane_prefix_cache_hits_total` |
| `DCGM_FI_DEV_FB_USED` | `modelplane_gpu_memory_used_bytes` |
| `DCGM_FI_DEV_GPU_TEMP` | `modelplane_gpu_temperature_celsius` |
| `DCGM_FI_DEV_POWER_USAGE` | `modelplane_gpu_power_watts` |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | `modelplane_gpu_tensor_active_ratio` |
| `envoy_cluster_upstream_rq_time` | `modelplane_frontend_request_duration_seconds` |

Some have no replacement. A rename carries one metric to one name, so the counters that
would fold several series under one label - tokens by direction, responses by reason,
requests by status - aren't part of this surface yet. Keep reading those from your engine
and your gateway directly. For prompt and output size, `modelplane_request_input_tokens`
and `modelplane_request_output_tokens` carry the same measurement as histograms.

`vllm:inter_token_latency_seconds` isn't renamed, because
SGLang publishes a metric of the same name measuring something else; use
`modelplane_frontend_tpot_seconds`, which the gateway measures the same way for every
engine. `DCGM_FI_DEV_GPU_UTIL` isn't renamed either, because it only tells you the card
wasn't idle; use `modelplane_gpu_compute_active_ratio` and
`modelplane_gpu_tensor_active_ratio`.

You can also defer the rewrite. A recording rule rebuilds an old name from a new one, so a
dashboard keeps working untouched while you migrate it:

```yaml
- record: vllm:time_to_first_token_seconds_bucket
  expr: label_replace(modelplane_request_ttft_seconds_bucket,
          "model_name", "$1", "deployment", "(.*)")
```

Load that into the Prometheus you already run and nothing on the dashboard changes. It's one
rule evaluation per metric over series your backend already holds, so it costs far less than
collecting everything twice. Write one per name in the table above, and delete them once the
panels use the new names.

A series no statement renames doesn't leave the cluster. If a panel needs an engine's
own name, write a `MetricMapping` that renames it onto the `modelplane_*` surface: a
mapping for an engine Modelplane already knows adds to the built-in renames rather
than replacing them.
<!-- vale write-good.Passive = YES -->
