import { useMutation } from '@tanstack/react-query';
import { useState, type ReactElement } from 'react';
import { Link } from 'react-router-dom';

import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  PageTransition,
  SectionTitle,
} from '@/components/ui';
import { fmt } from '@/lib/format';
import { streamCompletion } from '@/lib/api';
import { useTraceStore, type TraceEntry } from '@/lib/store';

/**
 * The waterfall.
 *
 * The stage list is the pipeline's real execution order, not a decoration —
 * if the pipeline changes, this changes with it. Bars are proportional to the
 * request's total duration so the LLM call visually dominates, which is the
 * honest picture: everything else is microseconds by comparison.
 */
function Waterfall({ trace }: { trace: TraceEntry }): ReactElement {
  const total = Math.max(trace.totalMs, 1);
  let offset = 0;

  return (
    <div className="space-y-1.5">
      {trace.stages.map((stage) => {
        const left = (offset / total) * 100;
        const width = Math.max(0.6, (stage.durationMs / total) * 100);
        offset += stage.durationMs;
        const isCall = stage.name === 'LLM call';

        return (
          <div key={stage.name} className="grid grid-cols-[11rem_1fr_4rem] items-center gap-2">
            <span className="truncate text-xs">{stage.name}</span>
            <div className="bg-muted/40 relative h-4 rounded">
              <div
                className={`absolute h-full rounded ${isCall ? 'bg-primary' : 'bg-primary/45'}`}
                style={{ left: `${String(left)}%`, width: `${String(width)}%` }}
              />
            </div>
            <span className="text-muted-foreground text-right font-mono text-xs tabular-nums">
              {stage.durationMs < 1 ? '<1' : Math.round(stage.durationMs)}ms
            </span>
          </div>
        );
      })}
      <p className="text-muted-foreground pt-1 text-xs">
        {trace.stages.map((s) => s.detail).join(' · ')}
      </p>
    </div>
  );
}

function Replay({ trace }: { trace: TraceEntry }): ReactElement {
  const [output, setOutput] = useState('');
  const [model, setModel] = useState<'student' | 'teacher'>('teacher');
  const record = useTraceStore((state) => state.record);

  const replay = useMutation({
    mutationFn: async () => {
      setOutput('');
      const result = await streamCompletion(
        { messages: [{ role: 'user', content: trace.prompt }], route_policy: model },
        (token) => {
          setOutput((current) => current + token);
        },
      );
      record(trace.prompt, result.meta, result.ttftMs, result.totalMs);
      return result;
    },
  });

  return (
    <Card className="mt-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Replay against</span>
        <div className="border-border flex items-center gap-1 rounded-md border p-0.5">
          {(['student', 'teacher'] as const).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => {
                setModel(option);
              }}
              className={`rounded px-2.5 py-1 text-xs capitalize ${
                model === option ? 'bg-muted font-medium' : 'text-muted-foreground'
              }`}
            >
              {option}
            </button>
          ))}
        </div>
        <Button
          variant="primary"
          disabled={replay.isPending}
          onClick={() => {
            replay.mutate();
          }}
        >
          {replay.isPending ? 'Replaying…' : 'Replay'}
        </Button>
      </div>

      {replay.isError && <ErrorState error={replay.error} />}

      {output && (
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <div className="text-muted-foreground mb-1 text-xs font-medium">
              Original ({trace.route ?? 'unknown'})
            </div>
            <div className="border-border bg-background max-h-48 overflow-y-auto whitespace-pre-wrap rounded border p-2 text-xs">
              {trace.prompt}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground mb-1 text-xs font-medium">Replay ({model})</div>
            <div className="border-border bg-background max-h-48 overflow-y-auto whitespace-pre-wrap rounded border p-2 text-xs">
              {output}
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

export function Traces(): ReactElement {
  const traces = useTraceStore((state) => state.traces);
  const clear = useTraceStore((state) => state.clear);
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Observability"
        title="Traces"
        subtitle="Every Playground call, with the pipeline stages the gateway actually executed"
        action={
          traces.length > 0 ? (
            <Button variant="ghost" onClick={clear}>
              Clear
            </Button>
          ) : undefined
        }
      />

      {traces.length === 0 ? (
        <EmptyState message="No traces yet. Send a prompt from the Playground and it will appear here." />
      ) : (
        <div className="space-y-3" data-testid="trace-list">
          {traces.map((trace) => (
            <Card key={trace.id} data-testid="trace-entry">
              <button
                type="button"
                className="flex w-full items-start justify-between gap-4 text-left"
                onClick={() => {
                  setExpanded(expanded === trace.id ? null : trace.id);
                }}
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">{trace.prompt}</p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    {trace.route && (
                      <Badge tone={trace.route === 'student' ? 'ok' : 'info'}>{trace.route}</Badge>
                    )}
                    {trace.task && <Badge tone="muted">{trace.task}</Badge>}
                    {trace.cacheHit && <Badge tone="warn">cache hit</Badge>}
                    {trace.fallbackReason && (
                      <Badge tone="danger">{trace.fallbackReason.replace(/_/g, ' ')}</Badge>
                    )}
                  </div>
                </div>
                <div className="shrink-0 text-right text-xs">
                  <div className="font-mono tabular-nums">
                    {trace.ttftMs === null ? '—' : fmt.ms(trace.ttftMs)} TTFT
                  </div>
                  <div className="text-muted-foreground font-mono tabular-nums">
                    {fmt.ms(trace.totalMs)} total
                  </div>
                  <div className="text-muted-foreground font-mono tabular-nums">
                    {trace.costUsd === null ? '—' : fmt.usd(trace.costUsd, 6)}
                  </div>
                </div>
              </button>

              {expanded === trace.id && (
                <div className="border-border mt-4 border-t pt-4">
                  <Waterfall trace={trace} />
                  {trace.traceId && (
                    <p className="text-muted-foreground mt-2 font-mono text-[10px]">
                      trace_id {trace.traceId}
                    </p>
                  )}
                  <Replay trace={trace} />
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      <p className="text-muted-foreground mt-6 text-xs">
        Traces are also exported as OTel GenAI spans to Langfuse when{' '}
        <code className="font-mono">LANGFUSE_*</code> is configured. Send a prompt from the{' '}
        <Link to="/playground" className="text-primary underline-offset-2 hover:underline">
          Playground
        </Link>
        .
      </p>
    </PageTransition>
  );
}
