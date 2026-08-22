/**
 * The frontend's stable import path for platform contracts.
 *
 * The declarations themselves are generated from `packages/schemas` by
 * `make schemas`, so the backend's Pydantic models are the only place a shape
 * is defined. Importing through this module rather than the generated file
 * means a regeneration never touches a call site, and CI's
 * `generate_ts_types.py --check` fails the build if the two ever drift.
 */
export type {
  Adapter,
  AdversarialResult,
  ChatCompletionChunk,
  ChatCompletionRequest,
  ChatCompletionResponse,
  ChatMessage,
  ChatRole,
  ClusterEvent,
  ClusterEventKind,
  CostBreakdown,
  DeploymentMode,
  DistillServeMetadata,
  EvalReport,
  GatewayError as GatewayErrorBody,
  GenerationMetrics,
  GpuNode,
  GpuState,
  HealthStatus,
  JudgeResult,
  LivenessResponse,
  ModelVersion,
  ParetoPoint,
  ReadinessCheck,
  ReadinessResponse,
  Rollout,
  RolloutEvent,
  RolloutSLI,
  RolloutStage,
  RouteDecision,
  RoutePolicy,
  RouteTarget,
  RuntimeEnvironment,
  ServiceIdentity,
  ServingSnapshot,
  SliceScore,
  SLIStatus,
  TenantUsage,
  TimeSeries,
  TimeSeriesPoint,
  TokenUsage,
} from './generated/schemas';

export { STAGE_TRAFFIC } from './generated/schemas';
