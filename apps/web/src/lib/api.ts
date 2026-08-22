import type {
  Adapter,
  ChatCompletionRequest,
  ClusterEvent,
  EvalReport,
  LivenessResponse,
  ModelVersion,
  ReadinessResponse,
  Rollout,
  RolloutEvent,
  ServingSnapshot,
  TenantUsage,
  TimeSeries,
} from './types';

/**
 * The single door between the console and the gateway.
 *
 * In development Vite proxies `/api/*`; in production Vercel rewrites the same
 * prefix to Render. So the base URL is a constant and no component ever needs
 * to know where the gateway lives.
 */
const API_PREFIX = '/api';

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = 'GatewayError';
  }
}

/** Bearer token for the session, held in memory plus localStorage. */
let authToken: string | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
  try {
    if (token) localStorage.setItem('distillserve.token', token);
    else localStorage.removeItem('distillserve.token');
  } catch {
    // Private windows and blocked site data throw here. The token still works
    // for this tab; it just will not survive a reload.
  }
}

export function loadStoredToken(): string | null {
  try {
    authToken = localStorage.getItem('distillserve.token');
  } catch {
    authToken = null;
  }
  return authToken;
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const base: Record<string, string> = { Accept: 'application/json', ...extra };
  if (authToken) base.Authorization = `Bearer ${authToken}`;
  return base;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers: headers(init?.headers as Record<string, string> | undefined),
  });

  // `/readyz` answers 503 with a full, useful body, so the body is read before
  // the status is judged.
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok && response.status !== 503) {
    const message =
      (body as { error?: { message?: string }; detail?: string } | null)?.error?.message ??
      (body as { detail?: string } | null)?.detail ??
      `${path} failed with ${String(response.status)}`;
    throw new GatewayError(message, response.status, body);
  }
  if (body === null) throw new GatewayError(`${path} returned a non-JSON body`, response.status);
  return body as T;
}

function post<T>(path: string, payload?: unknown): Promise<T> {
  // The body key is omitted rather than set to undefined: with
  // exactOptionalPropertyTypes, `undefined` is not a valid RequestInit body.
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
  });
}

export interface Identity {
  tenant_id: string;
  name: string;
  role: string;
  scopes: string[];
  requests_per_minute: number;
  monthly_budget_usd: number | null;
  authenticated: boolean;
}

export interface TelemetryInfo {
  stream: string;
  provenance: string;
}

export interface DashboardSummary {
  telemetry: TelemetryInfo;
  current: ServingSnapshot;
  tenants: TenantUsage[];
  events: ClusterEvent[];
}

export interface RolloutDetail {
  rollout: Rollout;
  audit: RolloutEvent[];
  audit_verified: boolean;
  audit_detail: string;
}

export const api = {
  health: () => request<LivenessResponse>('/healthz'),
  readiness: () => request<ReadinessResponse>('/readyz'),
  me: () => request<Identity>('/v1/me'),

  dashboard: () => request<DashboardSummary>('/v1/dashboard'),
  telemetry: () => request<TelemetryInfo>('/v1/telemetry'),
  series: (metric: string, window: string) =>
    request<TimeSeries>(
      `/v1/telemetry/series?metric=${encodeURIComponent(metric)}&window=${window}`,
    ),
  events: (limit = 50) => request<ClusterEvent[]>(`/v1/telemetry/events?limit=${String(limit)}`),

  adapters: () => request<Adapter[]>('/v1/adapters'),
  adapter: (id: string) => request<Adapter>(`/v1/adapters/${id}`),
  swapAdapter: (id: string, status: string) =>
    post<Adapter>(`/v1/adapters/${id}/status`, { status }),

  rollouts: () => request<Rollout[]>('/v1/rollouts'),
  rollout: (id: string) => request<RolloutDetail>(`/v1/rollouts/${id}`),
  advanceRollout: (id: string) => post<Rollout>(`/v1/rollouts/${id}/advance`),
  rollbackRollout: (id: string, reason: string) =>
    post<Rollout>(`/v1/rollouts/${id}/rollback`, { reason }),

  registry: () => request<ModelVersion[]>('/v1/registry'),
  modelVersion: (id: string) => request<ModelVersion>(`/v1/registry/${id}`),
  promote: (id: string) => post<ModelVersion>(`/v1/registry/${id}/promote`),

  evals: () => request<EvalReport[]>('/v1/evals'),
  evalReport: (id: string) => request<EvalReport>(`/v1/evals/${id}`),
};

/**
 * Open the dashboard's SSE stream.
 *
 * `EventSource` cannot send an Authorization header, so the token rides as a
 * query parameter when one is set. That is a deliberate, narrow exception:
 * the alternative is a `fetch`-based reader, and the extra complexity is not
 * worth it for a read-only telemetry feed.
 */
export function openTelemetryStream(onFrame: (frame: ServingSnapshot) => void): () => void {
  const suffix = authToken ? `?token=${encodeURIComponent(authToken)}` : '';
  const source = new EventSource(`${API_PREFIX}/v1/telemetry/stream${suffix}`);

  source.onmessage = (event: MessageEvent<string>) => {
    try {
      onFrame(JSON.parse(event.data) as ServingSnapshot);
    } catch {
      // A truncated frame is not worth tearing the stream down for.
    }
  };
  // EventSource reconnects on its own; logging every blip would be noise.
  source.onerror = () => undefined;

  return () => {
    source.close();
  };
}

export interface StreamedCompletion {
  text: string;
  ttftMs: number | null;
  totalMs: number;
  meta: Record<string, unknown> | null;
}

/**
 * Stream a chat completion, reporting TTFT measured in the browser.
 *
 * TTFT is measured here rather than taken from the response because the number
 * a user experiences includes the network, and a server-side measurement would
 * flatter the platform by exactly the round-trip time.
 */
export async function streamCompletion(
  body: ChatCompletionRequest,
  onToken: (token: string) => void,
): Promise<StreamedCompletion> {
  const started = performance.now();
  let ttftMs: number | null = null;
  let text = '';
  let meta: Record<string, unknown> | null = null;

  const response = await fetch(`${API_PREFIX}/v1/chat/completions`, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ ...body, stream: true }),
  });

  if (!response.ok || !response.body) {
    const detail: unknown = await response.json().catch(() => null);
    const message =
      (detail as { error?: { message?: string } } | null)?.error?.message ??
      `Completion failed with ${String(response.status)}`;
    throw new GatewayError(message, response.status, detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. Anything after the last
    // separator is a partial frame and stays in the buffer.
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';

    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      const payload = line.slice(6).trim();
      if (payload === '[DONE]') continue;

      const parsed = JSON.parse(payload) as {
        choices?: { delta?: { content?: string } }[];
        distillserve?: Record<string, unknown>;
        error?: { message?: string };
      };

      if (parsed.error) throw new GatewayError(parsed.error.message ?? 'stream error', 200, parsed);
      if (parsed.distillserve) meta = parsed.distillserve;

      const token = parsed.choices?.[0]?.delta?.content;
      if (token) {
        if (ttftMs === null) ttftMs = performance.now() - started;
        text += token;
        onToken(token);
      }
    }
  }

  return { text, ttftMs, totalMs: performance.now() - started, meta };
}
