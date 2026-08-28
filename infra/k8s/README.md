# Kubernetes manifests

Everything `DISTILLSERVE_MODE=self_hosted` needs to serve from your own GPUs:

| File                      | Purpose                                                                                                                                     |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `inferenceservice.yaml`   | KServe `InferenceService` running vLLM with FP8 weights + FP8 KV cache, EAGLE-3 speculative decoding and prefix caching.                    |
| `karpenter-nodepool.yaml` | NodePool mixing spot and on-demand H100 capacity, with a disruption budget that drains a reclaimed node before its 2-minute notice expires. |
| `keda-scaledobject.yaml`  | Scale on queue depth and p95 TTFT rather than CPU — GPU serving saturates on neither.                                                       |
| `hpa.yaml`                | HPA fallback for clusters without KEDA.                                                                                                     |
| `servicemonitor.yaml`     | Prometheus scrape of the gateway and of vLLM's own metrics.                                                                                 |

The cluster shape these encode — 8xH100, TP=2, spot-first — is the same shape recorded in
`data/reference/sources/catalog.yaml`, because every derived rate in the reference dataset is
computed against it.

```bash
kubectl apply -f infra/k8s/
export DISTILLSERVE_MODE=self_hosted
export VLLM_ENDPOINT=http://distillserve-student-predictor.distillserve.svc/v1
```

Three decisions worth reading the inline comments for:

1. **`--kv-cache-dtype=fp8`** — the KV cache is the memory bottleneck, and halving it roughly
   doubles the batch that fits. This is most of the $2.80 to $0.19 move.
2. **`consolidationPolicy: WhenEmpty`** — a 94-second cold start makes mid-generation node
   churn far more expensive than the idle capacity consolidation would reclaim.
3. **KEDA on queue depth and TTFT, never CPU** — a GPU serving pod saturates on KV cache and
   scheduler queue, so CPU-based autoscaling scales at the wrong time in both directions.

See [`apps/gateway/backends/self_hosted/README.md`](../../apps/gateway/src/distillserve_gateway/backends/self_hosted/README.md)
for what the backend assumes about this cluster.
