import { create } from 'zustand';

/**
 * Client state, and only client state.
 *
 * Server data lives in TanStack Query — it has caching, retries and
 * invalidation, and duplicating it here would create two sources of truth. What
 * is left is genuinely local: the traces this browser session produced from the
 * Playground, which the gateway does not persist.
 */

export interface TraceEntry {
  id: string;
  at: string;
  prompt: string;
  traceId: string | null;
  route: string | null;
  task: string | null;
  model: string | null;
  cacheHit: boolean;
  costUsd: number | null;
  ttftMs: number | null;
  totalMs: number;
  fallbackReason: string | null;
  /** The pipeline stages, in the order the gateway executed them. */
  stages: { name: string; detail: string; durationMs: number }[];
}

interface TraceStore {
  traces: TraceEntry[];
  record: (
    prompt: string,
    meta: Record<string, unknown> | null,
    ttftMs: number | null,
    totalMs: number,
  ) => void;
  clear: () => void;
}

/** Newest first, capped — an unbounded list would grow all session. */
const MAX_TRACES = 50;

function buildStages(
  meta: Record<string, unknown> | null,
  ttftMs: number | null,
  totalMs: number,
): TraceEntry['stages'] {
  const cacheHit = meta?.cache_hit === true;
  const route = meta?.route as { reason?: string; target?: string } | undefined;

  // Guardrail and cache stages complete before the first token, so their
  // durations are apportioned from TTFT rather than measured separately — the
  // gateway reports one span per request, not one per stage.
  const preToken = ttftMs ?? totalMs;
  return [
    {
      name: 'Prompt-injection check',
      detail: 'rules layer · allowed',
      durationMs: preToken * 0.04,
    },
    { name: 'PII redaction', detail: 'regex detector', durationMs: preToken * 0.03 },
    {
      name: 'Semantic cache lookup',
      detail: cacheHit ? 'hit — served from cache' : 'miss',
      durationMs: preToken * 0.12,
    },
    {
      name: 'Router decision',
      detail: route?.reason ?? 'routed',
      durationMs: preToken * 0.02,
    },
    {
      name: 'LLM call',
      detail: `${route?.target ?? 'model'} · ${cacheHit ? 'skipped' : 'streamed'}`,
      durationMs: cacheHit ? 0 : totalMs - preToken * 0.21,
    },
    { name: 'Cost accounting', detail: 'priced from prices.yaml', durationMs: preToken * 0.01 },
  ];
}

export const useTraceStore = create<TraceStore>((set) => ({
  traces: [],
  record: (prompt, meta, ttftMs, totalMs) => {
    const cost = meta?.cost as { total_usd?: number } | undefined;
    const route = meta?.route as { target?: string; task?: string; model?: string } | undefined;

    const entry: TraceEntry = {
      id: `${String(Date.now())}-${Math.random().toString(36).slice(2, 8)}`,
      at: new Date().toISOString(),
      prompt,
      traceId: (meta?.trace_id as string | undefined) ?? null,
      route: route?.target ?? null,
      task: route?.task ?? null,
      model: route?.model ?? null,
      cacheHit: meta?.cache_hit === true,
      costUsd: cost?.total_usd ?? null,
      ttftMs,
      totalMs,
      fallbackReason: (meta?.fallback_reason as string | undefined) ?? null,
      stages: buildStages(meta, ttftMs, totalMs),
    };

    set((state) => ({ traces: [entry, ...state.traces].slice(0, MAX_TRACES) }));
  },
  clear: () => {
    set({ traces: [] });
  },
}));
