---
title: Monitor the Fleet
weight: 37
aliases:

- /guides/telemetry/
description: Collect normalized metrics across the fleet and send them anywhere that speaks OTLP.
---
<!-- vale write-good.Passive = NO -->

Modelplane runs an OpenTelemetry collector on every inference cluster. It
collects from every component Modelplane installs. This includes the inference
server engine, inference gateway and Envoy proxy, router, and the GPU exporter
your cloud provides. It renames each component's series to a single
`modelplane_*` vocabulary and pushes to a collector on your control plane. That
collector is your fleet's single egress point and sends data to any
collector exporter backend.

Modelplane allows you to write one destination for your metrics. You don't need
to manage per-deployment configurations or update your configuration when a
deployment changes. The OpenTelemetry collector can find pods itself and leader/worker
splits or a prefill/decode pairs get collected the same as a single pod.

## Telemetry workflow

Every series carries `cluster`, `job`, and `instance` labels of the target
resource. A series about a deployment also carries `deployment`, `replica`,
`namespace`, `engine`, and `role` labels.

Each replica publishes its own series, so combine them in your query. Use the
aggregation that the metric's `acrossReplicas` field in its `MetricMapping` names:

 - `sum by (deployment)`, for anything counted, such as requests, tokens, or queue depth.
 - `avg by (deployment)`, for a ratio.
 - `max by (deployment)`, for a saturation figure an alert fires on.

To combine:

```promql
sum by (deployment) (rate(modelplane_frontend_request_duration_seconds_count[5m]))
```

The replica is an index rather than a pod, so it's bounded by the replica count
and survives a restart and a rolling update. Group by `replica`, not by `instance`.

For example:
```promql
# One line per replica, stable across rolling updates
max by (deployment, replica) (modelplane_kv_cache_utilization_ratio)
```
The `instance` label is the pod's address. Without the `instance` label two pods writing to the same
series would collide and a deployment with several pods per
replica or two gateway pods couldn't distinguish between the pods.

The `instance` label changes on a rolling update so queries that group by this
label gain a new series every time you deploy. Group by `replica` instead.

Some examples of the available metrics:

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
measured at the gateway. The engine's own series are what the engine spent. For
example, if the frontend metric is slow and the engine isn't, you can 
troubleshoot routing, queueing, or networking issues instead of the model.


Saturation gauges come as a pair. The average is what you plan capacity against; the `_max`
is what you alert on, because three replicas at 0.3 and one at 0.99 average to something
comfortable while the fourth evicts and recomputes. A high `_max` beside
`modelplane_requests_preempted_total` climbing is one replica thrashing.

For example:

```promql
max by (deployment) (modelplane_kv_cache_utilization_ratio) > 0.95
```

## Send telemetry to a destination

Create a `TelemetryDestination` for your OpenTelemetry-compatible endpoint:

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

To authenticate with a bearer token, store the token in a Secret in Modelplane's
namespace:

```shell
kubectl create secret generic telemetry-credentials \
  --namespace <modelplane-namespace> \
  --from-literal=token=<your-token>
```

Reference the Secret from the sink with `secretRef`, and set `auth.bearerTokenKey` to
the key that holds the token:

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

Modelplane configures the collector to send the token with every export.

The collector reads the token from a file rather than the environment.

If you run Prometheus, export to your Prometheus endpoint instead and query the fleet there:

```yaml
spec:
  sinks:
  - name: prometheus
    type: prometheus_remote_write
    endpoint: https://prom.example.internal/api/v1/write
```

If you create more than one sink, all get the entire stream. Each sink
carries it's own credential so you don't have to share a Secret.

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
    type: prometheus_remote_write
    endpoint: https://prom.example.internal/api/v1/write
```

That is two copies of the fleet's metrics, so a vendor charging per sample charges for
both.

Sinks can also come from more than one `TelemetryDestination`. Modelplane concatenates
them, so a team adding an export creates its own object rather than editing one somebody
else owns. Sink names are what the collector calls its exporters, so they have to be
unique across destinations; where two collide, the destination whose name sorts first
keeps it and Modelplane says so on the `ServingStack`.

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
Modelplane doesn't define a schema for an exporter's settings so anything under
the `config` is passed to the collector exactly as written. TLS, retries,
querying, compression and headers all work and any new settings in the collector
are respected and the sink keeps working. 

To use an authentication scheme Modelplane doesn't compose, define the extension
yourself under `spec.extensions`. Then reference it by its key from the sink's
`config.auth.authenticator`. The `auth` block does the same wiring for you when
you use a bearer token.

```yaml
spec:
  sinks:
  - name: vendor
    type: otlphttp
    endpoint: https://otel.vendor.example
    secretRef:
      name: vendor-oauth
    config:
      auth:
        authenticator: oauth2client/vendor
  extensions:
    oauth2client/vendor:
      client_id: modelplane
      client_secret: ${env:CLIENT_SECRET}
      token_url: https://issuer.example/oauth2/token
```

Modelplane doesn't run any collectors until you create a
`TelemetryDestination`.

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
  rate(modelplane_frontend_ttft_seconds_bucket{deployment="qwen3-8b"}[5m])))
```

Modelplane has no dashboards of its own. What it exports is counters and histogram buckets, and
your backend derives the rates and quantiles at query time. To precompute them instead,
export to Prometheus and write recording rules there.

## Engines

Modelplane renames vLLM's and SGLang's own metrics for you, so neither needs a mapping.
SGLang requires the `--enable-metrics` to publish them at all.


For example:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: my-sglang-model
  namespace: ml-team
spec:
  template:
    spec:
      engines:
      - name: engine
        members:
        - role: Standalone
          template:
            spec:
              containers:
              - name: engine
                image: lmsysorg/sglang:v0.5.10.post1-runtime
                command:
                - /bin/sh
                - -c
                - >-
                  exec python3 -m sglang.launch_server
                  --model-path <model>
                  --host 0.0.0.0
                  --port 8000
                  --enable-metrics
```

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
    acrossReplicas: Sum
  - from: my_engine_kv_transfer_ms
    fromUnit: Milliseconds
    to: modelplane_request_kv_transfer_seconds
    acrossReplicas: Mean
```

Modelplane renders every mapping into every cluster's collector, so you write one once.
`from` is the name your engine emits and `to` is what Modelplane calls it.

`acrossReplicas` says how a query should combine the metric over a deployment's replicas,
since every pod publishes its own series. Use `Sum` for anything counted and `Mean` for a
ratio, where adding two replicas at half capacity would read as one at full.

Say `fromUnit` whenever the engine measures in something other than the unit the name
claims, and Modelplane converts to the base one. Skipping this is the expensive mistake here:
a series named `_seconds` that holds milliseconds reads a thousand times fast, and nothing
downstream can tell.

Rename only where the measurements agree. Two engines' histograms under one name are worth
less than nothing if their buckets disagree, because a quantile over them is wrong rather
than approximate.

### Examples

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  metrics:
  # A plain rename.
  - from: my_engine_queued_requests
    to: modelplane_requests_waiting
    acrossReplicas: Sum

  # A unit conversion. The engine reports milliseconds; the name says seconds.
  - from: my_engine_kv_transfer_ms
    to: modelplane_request_kv_transfer_seconds
    fromUnit: Milliseconds
    acrossReplicas: Sum

  # A request count taken out of a duration histogram. The histogram
  # keeps its own name; this adds a counter beside it.
  - from: my_engine_request_duration_seconds
    part: Count
    to: modelplane_requests_total
    acrossReplicas: Sum

  # Two counters folded into one name, told apart by a fixed label.
  - from: my_engine_prompt_tokens_total
    to: modelplane_tokens_total
    acrossReplicas: Sum
    labels:
    - name: direction
      value: input
  - from: my_engine_generated_tokens_total
    to: modelplane_tokens_total
    acrossReplicas: Sum
    labels:
    - name: direction
      value: output

  # A label the engine already emits, renamed and its values translated.
  - from: my_engine_finished_requests_total
    to: modelplane_responses_total
    acrossReplicas: Sum
    labels:
    - name: reason
      from: finish_reason
      values:
        eos: stop
        max_tokens: length
```

## Why engine latency and gateway latency differ

`modelplane_request_ttft_seconds` comes from the engine, and engines bucket their
histograms differently. vLLM resolves down to a millisecond. SGLang resolves to a hundred
of them. A quantile across both is wrong, not approximate. Use the engine series to compare
one engine against itself, and the `frontend_` series for anything fleet-wide.

Some measurements don't translate at all. SGLang's inter-token latency isn't vLLM's time per
output token, so neither is renamed onto a shared name. The gateway measures time per output
token for both.
