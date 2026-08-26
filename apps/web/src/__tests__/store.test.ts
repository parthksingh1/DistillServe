import { beforeEach, describe, expect, it } from 'vitest';

import { useTraceStore } from '@/lib/store';

const meta = {
  trace_id: 'a'.repeat(32),
  route: { target: 'student', task: 'summarization', reason: 'distilled' },
  cache_hit: false,
  cost: { total_usd: 0.000091 },
  fallback_reason: null,
};

describe('trace store', () => {
  beforeEach(() => {
    useTraceStore.getState().clear();
  });

  it('records a completion as a trace', () => {
    useTraceStore.getState().record('Summarize this.', meta, 180, 900);

    const [trace] = useTraceStore.getState().traces;
    expect(trace?.route).toBe('student');
    expect(trace?.task).toBe('summarization');
    expect(trace?.costUsd).toBe(0.000091);
    expect(trace?.traceId).toHaveLength(32);
  });

  it('builds the waterfall in the pipeline execution order', () => {
    useTraceStore.getState().record('Summarize this.', meta, 180, 900);

    const names = useTraceStore.getState().traces[0]?.stages.map((stage) => stage.name);
    expect(names).toEqual([
      'Prompt-injection check',
      'PII redaction',
      'Semantic cache lookup',
      'Router decision',
      'LLM call',
      'Cost accounting',
    ]);
  });

  it('gives a cache hit a zero-duration LLM call', () => {
    useTraceStore.getState().record('x', { ...meta, cache_hit: true }, 12, 14);

    const llm = useTraceStore.getState().traces[0]?.stages.find((s) => s.name === 'LLM call');
    expect(llm?.durationMs).toBe(0);
    expect(llm?.detail).toContain('skipped');
  });

  it('puts newest traces first and caps the list', () => {
    for (let index = 0; index < 60; index += 1) {
      useTraceStore.getState().record(`prompt ${String(index)}`, meta, 10, 20);
    }

    const { traces } = useTraceStore.getState();
    expect(traces).toHaveLength(50);
    expect(traces[0]?.prompt).toBe('prompt 59');
  });

  it('tolerates a completion with no metadata', () => {
    useTraceStore.getState().record('x', null, null, 5);

    const [trace] = useTraceStore.getState().traces;
    expect(trace?.route).toBeNull();
    expect(trace?.stages).toHaveLength(6);
  });
});
