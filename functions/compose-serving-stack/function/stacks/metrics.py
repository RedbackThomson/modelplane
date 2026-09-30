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

# The GPUs, through whichever vendor's exporter the stack installed.
_GPU = {
    "DCGM_FI_DEV_FB_USED": "modelplane_gpu_memory_used_bytes",
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE": "modelplane_gpu_compute_active_ratio",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "modelplane_gpu_tensor_active_ratio",
    "DCGM_FI_PROF_DRAM_ACTIVE": "modelplane_gpu_memory_bandwidth_ratio",
    "DCGM_FI_DEV_GPU_TEMP": "modelplane_gpu_temperature_celsius",
    "DCGM_FI_DEV_POWER_USAGE": "modelplane_gpu_power_watts",
}


def _rename(pairs: dict[str, str]) -> list[str]:
    return [f'set(name, "{new}") where name == "{old}"' for old, new in pairs.items()]


def statements() -> list[str]:
    """Every rename Modelplane ships, as OTTL."""
    return (
        _rename(_GATEWAY)
        + _rename(_VLLM)
        + _rename(_SGLANG)
        + _rename(_PICKER)
        + _rename(_GPU)
        # DCGM counts energy in millijoules. The scale is here rather than left
        # to a query, because a name ending in _joules_total that holds
        # millijoules is the kind of thing nobody notices until a bill.
        + ['set(value_double, value_double / 1000) where name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"']
        + _rename({"DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "modelplane_energy_joules_total"})
    )


METRIC_STATEMENTS = statements()
