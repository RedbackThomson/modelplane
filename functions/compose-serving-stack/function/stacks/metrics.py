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

"""What Modelplane calls each metric the components it installs emit.

Reviewed, pinned data, the same discipline as the component lists. A
MetricMapping is the extension point for a component Modelplane ships no
statements for; these are the ones it does.

Renaming is only safe where the measurements agree. SGLang's
inter_token_latency is not vLLM's time per output token, so neither is
renamed onto a shared name and the gateway supplies that measurement for
both. A histogram is renamed only where its bucket boundaries match, which is
why SGLang's latency histograms are absent here: they resolve to a hundred
milliseconds where vLLM's resolve to one, and a quantile across the two is
wrong rather than approximate.
"""

from models.ai.modelplane.metricmapping import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The front door, which measures every request it proxies under the
# OpenTelemetry GenAI conventions, for whatever engine is behind it. These are
# the SLO metrics: one component, one bucket layout, so a fleet quantile over
# them is sound.
_GATEWAY = {
    "gen_ai_server_request_duration_seconds": "modelplane_frontend_request_duration_seconds",
    "gen_ai_server_time_to_first_token_seconds": "modelplane_frontend_ttft_seconds",
    "gen_ai_server_time_per_output_token_seconds": "modelplane_frontend_tpot_seconds",
}

# The engines, which explain what the gateway measured.
_VLLM = {
    "vllm:time_to_first_token_seconds": "modelplane_request_ttft_seconds",
    "vllm:e2e_request_latency_seconds": "modelplane_request_duration_seconds",
    "vllm:request_queue_time_seconds": "modelplane_request_queue_seconds",
    "vllm:request_prefill_time_seconds": "modelplane_request_prefill_seconds",
    "vllm:request_decode_time_seconds": "modelplane_request_decode_seconds",
    "vllm:request_prompt_tokens": "modelplane_request_input_tokens",
    "vllm:request_generation_tokens": "modelplane_request_output_tokens",
    "vllm:num_requests_running": "modelplane_requests_running",
    "vllm:num_requests_waiting": "modelplane_requests_waiting",
    "vllm:kv_cache_usage_perc": "modelplane_kv_cache_utilization_ratio",
    "vllm:num_preemptions_total": "modelplane_requests_preempted_total",
    "vllm:prefix_cache_hits_total": "modelplane_prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total": "modelplane_prefix_cache_lookups_total",
}

_SGLANG = {
    "sglang:queue_time_seconds": "modelplane_request_queue_seconds",
    "sglang:num_running_reqs": "modelplane_requests_running",
    "sglang:num_queue_reqs": "modelplane_requests_waiting",
    "sglang:token_usage": "modelplane_kv_cache_utilization_ratio",
    "sglang:num_retracted_requests_total": "modelplane_requests_preempted_total",
    "sglang:num_retracted_input_tokens_total": "modelplane_tokens_recomputed_total",
    "sglang:prompt_tokens_histogram": "modelplane_request_input_tokens",
    "sglang:generation_tokens_histogram": "modelplane_request_output_tokens",
}

# The endpoint picker. A router's queue is a different measurement from an
# engine's, so it keeps a name of its own.
_PICKER = {
    "llm_d_epp_scheduler_e2e_duration_seconds": "modelplane_route_decision_seconds",
}

# The GPUs, through whichever vendor's exporter the stack installed. DCGM
# reports framebuffer memory in MiB and energy in millijoules, and both are
# renamed onto a name that claims a different unit, so both say so.
_GPU = {
    "DCGM_FI_DEV_FB_USED": "modelplane_gpu_memory_used_bytes",
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE": "modelplane_gpu_compute_active_ratio",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "modelplane_gpu_tensor_active_ratio",
    "DCGM_FI_PROF_DRAM_ACTIVE": "modelplane_gpu_memory_bandwidth_ratio",
    "DCGM_FI_DEV_GPU_TEMP": "modelplane_gpu_temperature_celsius",
    "DCGM_FI_DEV_POWER_USAGE": "modelplane_gpu_power_watts",
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "modelplane_energy_joules_total",
}

_GPU_UNITS = {
    "DCGM_FI_DEV_FB_USED": "Mebibytes",
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "Millijoules",
}


# What describes a piece of hardware or a replica rather than a fleet. A ratio
# summed reads two replicas at half their KV cache as one at capacity, and a
# temperature summed is not a temperature at all.
#
# Everything else here is counted - requests, tokens, joules, watts drawn,
# queue depth - and a histogram can only be summed, which merges its buckets.
_MEAN = {
    "modelplane_kv_cache_utilization_ratio",
    "modelplane_gpu_compute_active_ratio",
    "modelplane_gpu_tensor_active_ratio",
    "modelplane_gpu_memory_bandwidth_ratio",
    "modelplane_gpu_temperature_celsius",
}


def _across_replicas(target: str) -> str:
    return "Mean" if target in _MEAN else "Sum"


def _mapping(
    name: str,
    pairs: dict[str, str],
    units: dict[str, str] | None = None,
) -> v1alpha1.MetricMapping:
    units = units or {}
    return v1alpha1.MetricMapping(
        metadata=metav1.ObjectMeta(name=name),
        spec=v1alpha1.Spec(
            metrics=[
                v1alpha1.Metric.model_validate(
                    {"from": src, "to": dst, "acrossReplicas": _across_replicas(dst)}
                    | ({"fromUnit": units[src]} if src in units else {})
                )
                for src, dst in pairs.items()
            ]
        ),
    )


def mappings() -> list[v1alpha1.MetricMapping]:
    """The mappings Modelplane provides, as the kind an operator would write.

    Built as MetricMappings rather than as collector configuration so that
    Modelplane's own renames reach the collector down the same path an
    operator's do, and a built-in that breaks breaks the path everyone uses.
    """
    return [
        _mapping("modelplane-gateway", _GATEWAY),
        _mapping("modelplane-vllm", _VLLM),
        _mapping("modelplane-sglang", _SGLANG),
        _mapping("modelplane-picker", _PICKER),
        _mapping("modelplane-gpu", _GPU, _GPU_UNITS),
    ]


BUILTIN_MAPPINGS = mappings()
