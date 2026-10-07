---
title: Llama-3.1-8B
weight: 40
description: An 8B dense chat model on one NVIDIA L4.
model: NousResearch/Meta-Llama-3.1-8B-Instruct
vendors: [Meta]
clouds: [EKS, GKE]
accelerators: [L4]
engines: [vLLM]
arch: Dense
precisions: ["BF16"]
size: 8B
ctx: "8,192"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai:v0.7.3]
gpuNote: 1× per node
---
<!-- vale write-good.Passive = NO -->
An 8B dense chat model on one NVIDIA L4. It's the entry recipe, with one
`Standalone` engine, no cache, and public weights from a Hugging Face mirror.
The deployment has no `clusterSelector`, so device capacity alone matches it to
any compatible L4 in the fleet.

This recipe was run end to end on GKE; the `InferenceClass`, `InferenceCluster`,
and `ModelDeployment` are the exact manifests from that run. The EKS platform
shape is the standard single-L4 recipe. It passes server validation but was not
served in this run. Apply the platform side first, then the ML side. Edit the
GCP project placeholder in the GKE `InferenceCluster` before applying it.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< tabs >}}
{{< tab "EKS" >}}
{{< manifests "recipes/llama-3.1-8b/inference-class-eks.yaml" >}}

{{< manifests "recipes/llama-3.1-8b/inference-cluster-eks.yaml" >}}
{{< /tab >}}
{{< tab "GKE" >}}
{{< manifests "recipes/llama-3.1-8b/inference-class-gke.yaml" >}}

{{< manifests "recipes/llama-3.1-8b/inference-cluster-gke.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

## Deployment

{{< manifests "recipes/llama-3.1-8b/model-deployment.yaml" >}}

{{< manifests "recipes/llama-3.1-8b/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
