/**
 * GENERATED FILE — do not edit.
 *
 * Emitted from packages/schemas by `make schemas`. Edit the Pydantic models
 * and regenerate; CI runs `generate_ts_types.py --check` and fails on drift.
 */

/**
 * Author of a chat message.
 */
export type ChatRole = 'system' | 'user' | 'assistant' | 'tool';

/**
 * Something that happened to the serving pool.
 */
export type ClusterEventKind = 'scale_up' | 'scale_down' | 'spot_reclaim' | 'cold_start' | 'rollout_advance' | 'rollout_rollback' | 'sli_breach';

/**
 * How this DistillServe instance sources inference and telemetry.
 *
 * The three modes are peers, not a ladder — they differ only in which
 * ``InferenceBackend`` / ``TelemetryStream`` pair is bound at startup. Every
 * other code path (routing, cache, guardrails, cost, evals, rollouts) is
 * shared verbatim.
 *
 * Attributes:
 * HOSTED: Route to hosted providers through LiteLLM. Needs only API keys.
 * SELF_HOSTED: Route to a vLLM OpenAI-compatible endpoint on a GPU
 * cluster provisioned from ``infra/k8s/``.
 * SANDBOX: Generation is served by the hosted backend while
 * ``self_hosted`` telemetry is streamed from the reference benchmark
 * dataset in ``data/reference/``.
 */
export type DeploymentMode = 'hosted' | 'self_hosted' | 'sandbox';

/**
 * Lifecycle state of a GPU node in the serving pool.
 */
export type GpuState = 'provisioning' | 'warming' | 'serving' | 'draining' | 'reclaimed';

/**
 * Outcome of a health or readiness probe.
 */
export type HealthStatus = 'ok' | 'degraded' | 'failed';

/**
 * States of the canary state machine.
 *
 * Ordered. ``advance`` moves one step right; ``rollback`` jumps directly to
 * ``rolled_back`` from anywhere, because a quality incident is not something
 * you unwind one stage at a time.
 */
export type RolloutStage = 'shadow' | 'canary_10' | 'canary_50' | 'full' | 'rolled_back';

/**
 * How the caller wants the route chosen.
 *
 * ``AUTO`` defers to the task classifier. The explicit values exist because
 * the Playground's Compare mode, and any A/B harness, must be able to pin a
 * side of the comparison rather than argue with the router.
 */
export type RoutePolicy = 'auto' | 'teacher' | 'student';

/**
 * Where a request may be sent.
 *
 * ``ADAPTER`` is a family rather than a single destination: the concrete LoRA
 * is named in :attr:`RouteDecision.adapter`.
 */
export type RouteTarget = 'teacher' | 'student' | 'adapter';

/**
 * Deployment environment, used for log routing and trace attributes.
 */
export type RuntimeEnvironment = 'local' | 'preview' | 'production' | 'test';

/**
 * Whether a service-level indicator is within its objective.
 */
export type SLIStatus = 'healthy' | 'at_risk' | 'breached';

/**
 * A LoRA adapter served by the platform.
 */
export interface Adapter {
  id: string;
  name: string;
  task: string;
  base_model: string;
  rank: number;
  alpha: number;
  quality_score: number;
  /**
   * Weighted parity against the teacher.
   */
  parity: number;
  request_share: number;
  requests_24h: number;
  trained_at: string;
  training_examples: number;
  /**
   * active | shadow | retired
   */
  status: string;
  artifact_uri: string;
  slices: SliceScore[];
}

/**
 * One adversarial suite's outcome.
 */
export interface AdversarialResult {
  /**
   * prompt_injection | jailbreak | distribution_shift
   */
  suite: string;
  attempts: number;
  blocked: number;
  passed_through: number;
}

/**
 * One completion choice in a non-streaming response.
 */
export interface ChatCompletionChoice {
  index: number;
  message: ChatMessage;
  finish_reason: string | null;
}

/**
 * One SSE frame, OpenAI-shaped.
 *
 * ``distillserve`` is attached only to the final frame: usage, cost and TTFT
 * are not known until the stream ends, and emitting placeholder values on
 * every frame would invite a client to read them mid-stream.
 */
export interface ChatCompletionChunk {
  id: string;
  object: "chat.completion.chunk";
  created: number;
  model: string;
  choices: ChatCompletionChunkChoice[];
  usage: TokenUsage | null;
  distillserve: DistillServeMetadata | null;
}

/**
 * One choice within a streaming chunk.
 */
export interface ChatCompletionChunkChoice {
  index: number;
  delta: ChatCompletionDelta;
  finish_reason: string | null;
}

/**
 * Incremental message content in a streaming chunk.
 */
export interface ChatCompletionDelta {
  role: ChatRole | null;
  content: string | null;
}

/**
 * Request body for ``POST /v1/chat/completions``.
 *
 * ``extra="allow"`` so that OpenAI parameters this gateway does not yet
 * interpret (``tools``, ``response_format``, …) survive the round trip to the
 * provider instead of being silently dropped.
 */
export interface ChatCompletionRequest {
  /**
   * Model id. Omit to let the router choose; a concrete id pins the route.
   */
  model?: string | null;
  messages: ChatMessage[];
  stream?: boolean;
  temperature?: number | null;
  max_tokens?: number | null;
  top_p?: number | null;
  stop?: string[] | string | null;
  /**
   * Opaque end-user id, forwarded upstream.
   */
  user?: string | null;
  /**
   * Override the router. `auto` uses the task classifier.
   */
  route_policy?: RoutePolicy;
  /**
   * Skip classification by declaring the task yourself.
   */
  task?: string | null;
  /**
   * Tenant id for rate limiting and per-tenant cost attribution.
   */
  tenant?: string | null;
}

/**
 * Non-streaming response body, OpenAI-shaped plus ``distillserve``.
 */
export interface ChatCompletionResponse {
  id: string;
  object: "chat.completion";
  /**
   * Unix timestamp, seconds.
   */
  created: number;
  model: string;
  choices: ChatCompletionChoice[];
  usage: TokenUsage;
  distillserve: DistillServeMetadata;
}

/**
 * One message in a conversation.
 */
export interface ChatMessage {
  role: ChatRole;
  /**
   * Message text. Multimodal parts are not yet supported.
   */
  content: string;
  /**
   * Optional author name.
   */
  name?: string | null;
}

/**
 * One timeline entry on the GPU pool chart.
 */
export interface ClusterEvent {
  id: string;
  kind: ClusterEventKind;
  at: string;
  message: string;
  node: string | null;
  /**
   * info | warning | critical
   */
  severity: string;
  detail: Record<string, string>;
}

/**
 * What a completion cost, and how that was computed.
 *
 * Both hosted and self-hosted costs land in ``total_usd`` so every dashboard
 * and eval can compare them directly — that comparison is the platform's
 * central claim, and it only works if one number means the same thing in both
 * modes. ``basis`` records which formula produced it.
 */
export interface CostBreakdown {
  total_usd: number;
  input_usd: number;
  output_usd: number;
  /**
   * Hosted providers bill per token; self-hosted serving bills per GPU-second.
   */
  basis: "per_token" | "per_gpu_second";
  /**
   * Version of prices.yaml used, for auditability.
   */
  price_sheet_version: string;
}

/**
 * DistillServe's additions to an OpenAI-shaped response.
 */
export interface DistillServeMetadata {
  /**
   * OTel trace id, for deep-linking into Langfuse.
   */
  trace_id: string;
  route: RouteDecision;
  /**
   * Whether the semantic cache served this response.
   */
  cache_hit: boolean;
  cost: CostBreakdown;
  metrics: GenerationMetrics;
  /**
   * Backend that served generation, e.g. 'hosted'.
   */
  backend: string;
  /**
   * Deployment mode in effect.
   */
  mode: string;
  /**
   * Set when the primary path failed and a fallback served the request.
   */
  fallback_reason: string | null;
}

/**
 * Everything the Eval Reports page shows for one model version.
 */
export interface EvalReport {
  model_version_id: string;
  model_name: string;
  generated_at: string;
  dataset_version: string;
  case_count: number;
  overall_parity: number;
  slices: SliceScore[];
  judge: JudgeResult[];
  adversarial: AdversarialResult[];
  pareto: ParetoPoint[];
  /**
   * Minimum per-slice parity required to pass the CI gate.
   */
  gate_threshold: number;
  judge_model: string;
  /**
   * Calibration-set agreement drift since the judge was last pinned.
   */
  judge_drift: number;
}

/**
 * Error response envelope.
 */
export interface GatewayError {
  error: GatewayErrorBody;
}

/**
 * Structured error payload.
 *
 * Shaped like OpenAI's error envelope so a client's existing error handling
 * keeps working, with ``fallback_reason`` added for the degradation case: a
 * provider rate limit that could not be served from cache is a different
 * operational event from a malformed request, and the trace records which.
 */
export interface GatewayErrorBody {
  message: string;
  /**
   * Error class, e.g. 'rate_limit_error'.
   */
  type: string;
  code: string | null;
  trace_id: string | null;
  fallback_reason: string | null;
}

/**
 * Latency measurements for one completion.
 *
 * TTFT and ITL are reported separately because they answer different product
 * questions: TTFT is what a user perceives as responsiveness, ITL is what
 * determines whether a long answer feels smooth. An average of the two hides
 * both.
 */
export interface GenerationMetrics {
  /**
   * Time to first token; null for non-streaming calls.
   */
  ttft_ms: number | null;
  /**
   * Wall-clock duration of the completion.
   */
  total_ms: number;
  /**
   * Decode throughput, once at least one token has arrived.
   */
  output_tokens_per_second: number | null;
}

/**
 * A single accelerator in the serving pool.
 */
export interface GpuNode {
  name: string;
  state: GpuState;
  /**
   * spot | on-demand
   */
  capacity_type: string;
  utilization: number;
  kv_cache_utilization: number;
  memory_used_gb: number;
  memory_total_gb: number;
  running_requests: number;
  waiting_requests: number;
}

/**
 * Pairwise LLM-as-judge outcome for one slice.
 */
export interface JudgeResult {
  slice: string;
  student_wins: number;
  teacher_wins: number;
  ties: number;
}

/**
 * Body returned by ``GET /healthz``.
 *
 * Liveness answers "is this process healthy enough to keep running", so it
 * deliberately touches no dependency: a Redis outage must not cause an
 * orchestrator to restart-loop a gateway that is otherwise fine.
 */
export interface LivenessResponse {
  status: HealthStatus;
  identity: ServiceIdentity;
  /**
   * Seconds since app startup.
   */
  uptime_seconds: number;
}

/**
 * A model run, with everything needed to reproduce it.
 */
export interface ModelVersion {
  id: string;
  name: string;
  /**
   * teacher | student | quantized | dpo
   */
  family: string;
  base_model: string;
  version: string;
  created_at: string;
  git_sha: string;
  dataset_version: string;
  seed: number;
  hyperparameters: Record<string, string>;
  quality_score: number;
  parity: number;
  p95_ttft_ms: number;
  usd_per_million_tokens: number;
  /**
   * promoted | candidate | archived
   */
  status: string;
  promotion_history: string[];
}

/**
 * One model on the latency/cost/quality frontier.
 */
export interface ParetoPoint {
  model: string;
  quality: number;
  p95_ttft_ms: number;
  usd_per_million_tokens: number;
  on_frontier: boolean;
}

/**
 * Result of one dependency probe contributing to readiness.
 */
export interface ReadinessCheck {
  /**
   * Dependency identifier, e.g. 'redis'.
   */
  name: string;
  status: HealthStatus;
  /**
   * Whether a failure of this check makes the service unready. Optional dependencies degrade the service instead of failing it.
   */
  required: boolean;
  /**
   * Human-readable diagnostic.
   */
  detail: string | null;
  /**
   * Probe duration.
   */
  latency_ms: number | null;
}

/**
 * Body returned by ``GET /readyz``.
 *
 * Readiness answers "should this process receive traffic", so it *does* touch
 * dependencies. The aggregate is ``failed`` if any required check failed,
 * ``degraded`` if only optional checks failed, otherwise ``ok``.
 */
export interface ReadinessResponse {
  status: HealthStatus;
  identity: ServiceIdentity;
  checks: ReadinessCheck[];
}

/**
 * A candidate model working its way toward full traffic.
 */
export interface Rollout {
  id: string;
  name: string;
  candidate_model: string;
  baseline_model: string;
  stage: RolloutStage;
  traffic_percent: number;
  started_at: string;
  updated_at: string;
  auto_rollback: boolean;
  slis: RolloutSLI[];
  notes: string;
}

/**
 * An entry in a rollout's audit trail.
 *
 * ``signature`` is an HMAC over the entry's content chained to the previous
 * entry's signature. A tampered or removed row breaks the chain, which is
 * what makes this an audit log rather than a log.
 */
export interface RolloutEvent {
  id: string;
  rollout_id: string;
  at: string;
  kind: string;
  from_stage: RolloutStage | null;
  to_stage: RolloutStage | null;
  actor: string;
  reason: string;
  signature: string;
  previous_signature: string | null;
}

/**
 * One gate the rollout is judged against.
 */
export interface RolloutSLI {
  name: string;
  value: number;
  threshold: number;
  unit: string;
  /**
   * gte (higher is better) | lte (lower is better)
   */
  comparison: string;
  status: SLIStatus;
}

/**
 * Why a request went where it went.
 *
 * Recorded on the span and echoed to the client. A route that cannot explain
 * itself is impossible to debug once it is serving production traffic, so
 * ``reason`` and ``confidence`` are required rather than optional colour.
 */
export interface RouteDecision {
  target: RouteTarget;
  /**
   * Concrete model id the request was sent to.
   */
  model: string;
  /**
   * LoRA adapter name, when applicable.
   */
  adapter: string | null;
  /**
   * Task label assigned by the classifier, e.g. 'summarization'.
   */
  task: string;
  /**
   * Policy that produced this decision.
   */
  policy: RoutePolicy;
  /**
   * Classifier confidence in ``task``.
   */
  confidence: number;
  /**
   * Human-readable justification, shown in the trace waterfall.
   */
  reason: string;
}

/**
 * Who this process is and which commit it was built from.
 *
 * ``git_sha`` is resolved at import time from the platform-provided build env
 * var, falling back to the working tree. It is echoed on every probe so a
 * dashboard number can always be traced to the exact code that produced it.
 */
export interface ServiceIdentity {
  /**
   * Service name, e.g. 'gateway'.
   */
  service: string;
  /**
   * Semantic version of the service package.
   */
  version: string;
  /**
   * Full commit SHA, or 'unknown' outside a checkout.
   */
  git_sha: string;
  /**
   * First 12 characters of `git_sha`.
   */
  git_sha_short: string;
  /**
   * Active deployment mode.
   */
  mode: DeploymentMode;
  /**
   * Deployment environment.
   */
  environment: RuntimeEnvironment;
}

/**
 * One instant of serving telemetry.
 *
 * This is the frame the Live Dashboard's SSE stream emits. Identical in shape
 * whether it came from a real vLLM ``/metrics`` scrape or the reference
 * dataset — that equivalence is the design constraint the whole sandbox mode
 * is built around.
 */
export interface ServingSnapshot {
  at: string;
  output_tokens_per_second: number;
  requests_per_second: number;
  ttft_p50_ms: number;
  ttft_p95_ms: number;
  ttft_p99_ms: number;
  /**
   * Inter-token latency, median.
   */
  itl_p50_ms: number;
  itl_p95_ms: number;
  usd_per_million_tokens: number;
  cache_hit_rate: number;
  gpu_utilization: number;
  kv_cache_utilization: number;
  queue_depth: number;
  active_nodes: number;
  spot_nodes: number;
  /**
   * EAGLE-3 draft-token acceptance rate.
   */
  acceptance_rate: number | null;
}

/**
 * Quality on one evaluation slice.
 */
export interface SliceScore {
  slice: string;
  student_score: number;
  teacher_score: number;
  sample_count: number;
}

/**
 * Per-tenant slice of the dashboard's breakdown table.
 */
export interface TenantUsage {
  tenant_id: string;
  name: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cache_hit_rate: number;
  p95_ttft_ms: number;
  /**
   * Fraction of this tenant's traffic served by the student.
   */
  student_share: number;
}

/**
 * A named series over a window.
 */
export interface TimeSeries {
  metric: string;
  unit: string;
  /**
   * 1h | 24h | 7d
   */
  window: string;
  points: TimeSeriesPoint[];
  /**
   * Provenance, resolvable in the reference catalog.
   */
  source_id: string;
}

/**
 * One point on a dashboard chart.
 */
export interface TimeSeriesPoint {
  at: string;
  value: number;
}

/**
 * Token counts for one completion.
 */
export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
}

export const STAGE_TRAFFIC: Record<RolloutStage, number> = {
  'shadow': 0,
  'canary_10': 10,
  'canary_50': 50,
  'full': 100,
  'rolled_back': 0,
};
