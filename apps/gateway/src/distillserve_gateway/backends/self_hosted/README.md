# Self-hosted serving

`SelfHostedInferenceBackend` targets a vLLM OpenAI-compatible endpoint. This
document records the cluster shape it assumes, because the backend's behaviour
only makes sense against a specific one.

## Assumed cluster

|                      |                                                                 |
| -------------------- | --------------------------------------------------------------- |
| Accelerator          | 8 × NVIDIA H100 80GB SXM                                        |
| Topology             | 4 replicas × tensor-parallel 2                                  |
| Serving engine       | vLLM 0.9.x, OpenAI-compatible server                            |
| Weights              | FP8                                                             |
| KV cache             | FP8 (`--kv-cache-dtype=fp8`)                                    |
| Speculative decoding | EAGLE-3, 3 draft tokens                                         |
| Prefix caching       | Enabled                                                         |
| Adapters             | 12 LoRA modules, max rank 32, all resident                      |
| Scaling              | Karpenter spot-first NodePool, KEDA on queue depth and p95 TTFT |

Manifests are in [`infra/k8s/`](../../../../../../infra/k8s/).

## What the backend assumes

**An adapter is a model name.** vLLM matches the `model` field against its
loaded LoRA modules before its base model, so selecting an adapter is a request
parameter. That is why `GenerationRequest.adapter` exists and why hot-swap is an
API call rather than a rollout.

**Usage is always reported.** vLLM returns exact prompt and completion token
counts on the final SSE frame when asked with `stream_options.include_usage`, so
unlike the hosted path there is never an estimate. Self-hosted cost accounting
is therefore exact where hosted sometimes is not.

**429 means the queue is full.** vLLM returns 429 when its request queue is
saturated. That is genuinely the same condition as a hosted provider's rate
limit — retryable, and worth serving from cache — so it is classified the same
way and triggers the same degradation path.

**Cost is per GPU-second, derived.** There is no per-token bill. The telemetry
stream computes `$/M tokens` from the pool rate and observed throughput, with
utilisation in the denominator because idle GPU-seconds are billed too.

## Enabling it

```bash
export DISTILLSERVE_MODE=self_hosted
export VLLM_ENDPOINT=http://distillserve-student-predictor.distillserve.svc/v1
```

The gateway fails at startup if `VLLM_ENDPOINT` is unset in this mode — the
invariant is checked at boot rather than on the first request an hour later.

The code path is **not** gated behind the mode: setting `VLLM_ENDPOINT` in any
mode makes it reachable, so it can be smoke-tested against a laptop vLLM without
reconfiguring the platform.

## Running vLLM locally to test against

```bash
docker run --gpus all -p 8000:8000 \
  vllm/vllm-openai:v0.9.1 \
  --model meta-llama/Llama-3.2-1B-Instruct \
  --served-model-name distillserve-student \
  --enable-prefix-caching
```

A 1B model on one consumer GPU exercises every code path in this backend —
streaming, usage reporting, error classification, `/models` listing. It will not
reproduce the reference deployment's numbers, and is not meant to.
