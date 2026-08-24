import { useMutation } from '@tanstack/react-query';
import { useState, type ReactElement } from 'react';
import { useSearchParams } from 'react-router-dom';

import {
  Badge,
  Button,
  Card,
  ErrorState,
  PageTransition,
  SectionTitle,
  Stat,
} from '@/components/ui';
import { fmt } from '@/lib/format';
import { streamCompletion, type StreamedCompletion } from '@/lib/api';
import { useTraceStore } from '@/lib/store';
import type { RoutePolicy } from '@/lib/types';

const SAMPLES = [
  {
    task: 'summarization',
    label: 'Summarize a support thread',
    prompt:
      'Summarize this support thread in two sentences:\n\nCustomer: My export keeps failing at 80%.\nAgent: Which browser?\nCustomer: Chrome, latest. It worked last week.\nAgent: We shipped a change to the export worker on Tuesday — rolling back now.\nCustomer: Thanks, it works.',
  },
  {
    task: 'extraction',
    label: 'Extract fields as JSON',
    prompt:
      'Extract the invoice number, total and due date as JSON:\n\nInvoice INV-4471 for $1,284.50 is due on 30 September 2026. Remit to Northwind Ltd.',
  },
  {
    task: 'sql_generation',
    label: 'Write a SQL query',
    prompt:
      'Write a SQL query returning monthly active users for the last 6 months from a table `events(user_id, occurred_at)`.',
  },
  {
    task: 'reasoning',
    label: 'Multi-step reasoning (escalates)',
    prompt:
      'A pool is filled by pipe A in 6 hours and by pipe B in 4 hours. Pipe C empties it in 12 hours. All three run together. Reason step by step to find how long the pool takes to fill.',
  },
];

interface Result extends StreamedCompletion {
  label: string;
}

function meta(result: Result | null, key: string): unknown {
  return result?.meta?.[key];
}

function ResultPanel({
  title,
  result,
  streaming,
  text,
}: {
  title: string;
  result: Result | null;
  streaming: boolean;
  text: string;
}): ReactElement {
  const route = meta(result, 'route') as
    { target?: string; task?: string; reason?: string } | undefined;
  const cost = meta(result, 'cost') as { total_usd?: number } | undefined;
  const cacheHit = meta(result, 'cache_hit') === true;

  return (
    <Card className="flex min-h-[22rem] flex-col">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-medium">{title}</h3>
        <div className="flex items-center gap-1.5">
          {route?.target && (
            <Badge tone={route.target === 'student' ? 'ok' : 'info'}>{route.target}</Badge>
          )}
          {cacheHit && <Badge tone="warn">cache hit</Badge>}
          {streaming && <Badge tone="muted">streaming…</Badge>}
        </div>
      </div>

      <div
        className="border-border bg-background flex-1 overflow-y-auto whitespace-pre-wrap rounded border p-3 text-sm"
        data-testid={`output-${title.toLowerCase().replace(/\s+/g, '-')}`}
      >
        {text || <span className="text-muted-foreground">Awaiting output…</span>}
      </div>

      {result && (
        <div className="mt-3 grid grid-cols-4 gap-2 text-xs">
          <div>
            <div className="text-muted-foreground">TTFT</div>
            <div className="font-mono tabular-nums">
              {result.ttftMs === null ? '—' : fmt.ms(result.ttftMs)}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">Total</div>
            <div className="font-mono tabular-nums">{fmt.ms(result.totalMs)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">Cost</div>
            <div className="font-mono tabular-nums">
              {cost?.total_usd === undefined ? '—' : fmt.usd(cost.total_usd, 6)}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">Task</div>
            <div className="font-mono">{route?.task ?? '—'}</div>
          </div>
        </div>
      )}

      {route?.reason && <p className="text-muted-foreground mt-2 text-xs">Route: {route.reason}</p>}
    </Card>
  );
}

export function Playground(): ReactElement {
  const [params] = useSearchParams();
  const [prompt, setPrompt] = useState(SAMPLES[0]?.prompt ?? '');
  const [policy, setPolicy] = useState<RoutePolicy>('auto');
  const [compare, setCompare] = useState(params.get('mode') === 'compare');

  const [primaryText, setPrimaryText] = useState('');
  const [secondaryText, setSecondaryText] = useState('');
  const [primary, setPrimary] = useState<Result | null>(null);
  const [secondary, setSecondary] = useState<Result | null>(null);

  const recordTrace = useTraceStore((state) => state.record);

  const run = useMutation({
    mutationFn: async () => {
      setPrimaryText('');
      setSecondaryText('');
      setPrimary(null);
      setSecondary(null);

      const messages = [{ role: 'user' as const, content: prompt }];

      if (!compare) {
        const result = await streamCompletion({ messages, route_policy: policy }, (token) => {
          setPrimaryText((current) => current + token);
        });
        const labelled = { ...result, label: 'Auto-routed' };
        setPrimary(labelled);
        recordTrace(prompt, labelled.meta, labelled.ttftMs, labelled.totalMs);
        return;
      }

      // Compare mode runs both sides in parallel: sequential calls would make
      // the second side's latency include the first's, which is exactly the
      // number the comparison is meant to measure.
      const [student, teacher] = await Promise.all([
        streamCompletion({ messages, route_policy: 'student' }, (token) => {
          setPrimaryText((current) => current + token);
        }),
        streamCompletion({ messages, route_policy: 'teacher' }, (token) => {
          setSecondaryText((current) => current + token);
        }),
      ]);

      const studentResult = { ...student, label: 'Student' };
      const teacherResult = { ...teacher, label: 'Teacher' };
      setPrimary(studentResult);
      setSecondary(teacherResult);
      recordTrace(prompt, studentResult.meta, studentResult.ttftMs, studentResult.totalMs);
      recordTrace(prompt, teacherResult.meta, teacherResult.ttftMs, teacherResult.totalMs);
    },
  });

  const studentCost = (meta(primary, 'cost') as { total_usd?: number } | undefined)?.total_usd;
  const teacherCost = (meta(secondary, 'cost') as { total_usd?: number } | undefined)?.total_usd;
  const savings =
    studentCost !== undefined && teacherCost !== undefined && teacherCost > 0
      ? 1 - studentCost / teacherCost
      : null;

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Inference"
        title="Playground"
        subtitle="Send a prompt through the real pipeline. Every call appears on Traces."
      />

      <Card className="mb-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {SAMPLES.map((sample) => (
            <Button
              key={sample.task}
              variant="ghost"
              onClick={() => {
                setPrompt(sample.prompt);
              }}
            >
              {sample.label}
            </Button>
          ))}
        </div>

        <textarea
          value={prompt}
          onChange={(event) => {
            setPrompt(event.target.value);
          }}
          rows={6}
          data-testid="prompt-input"
          className="border-border bg-background focus:border-primary/50 w-full resize-y rounded border p-3 font-mono text-sm outline-none"
          placeholder="Ask something…"
        />

        <div className="mt-3 flex flex-wrap items-center gap-3">
          <div className="border-border flex items-center gap-1 rounded-md border p-0.5">
            {(['auto', 'student', 'teacher'] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => {
                  setPolicy(option);
                }}
                disabled={compare}
                className={`rounded px-2.5 py-1 text-xs capitalize transition-colors disabled:opacity-40 ${
                  policy === option ? 'bg-muted font-medium' : 'text-muted-foreground'
                }`}
              >
                {option}
              </button>
            ))}
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={compare}
              onChange={(event) => {
                setCompare(event.target.checked);
              }}
              data-testid="compare-toggle"
            />
            Compare mode
          </label>

          <Button
            variant="primary"
            onClick={() => {
              run.mutate();
            }}
            disabled={run.isPending || prompt.trim().length === 0}
            data-testid="send-prompt"
          >
            {run.isPending ? 'Streaming…' : compare ? 'Compare both' : 'Send'}
          </Button>
        </div>
      </Card>

      {run.isError && <ErrorState error={run.error} />}

      {savings !== null && (
        <div className="mb-4 grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat
            label="Cost delta"
            value={fmt.pct(savings)}
            hint="student vs teacher"
            trend="down"
            lowerIsBetter
          />
          <Stat
            label="TTFT delta"
            value={
              primary?.ttftMs !== null && primary?.ttftMs !== undefined && secondary?.ttftMs
                ? fmt.ms(secondary.ttftMs - primary.ttftMs)
                : '—'
            }
            hint="teacher minus student"
            trend="down"
            lowerIsBetter
          />
          <Stat
            label="Tokens"
            value={
              (
                meta(primary, 'metrics') as { output_tokens_per_second?: number } | undefined
              )?.output_tokens_per_second?.toFixed(0) ?? '—'
            }
            hint="student tok/s"
          />
        </div>
      )}

      <div className={compare ? 'grid gap-4 lg:grid-cols-2' : ''}>
        <ResultPanel
          title={compare ? 'Student' : 'Output'}
          result={primary}
          streaming={run.isPending && primaryText.length === 0}
          text={primaryText}
        />
        {compare && (
          <ResultPanel
            title="Teacher"
            result={secondary}
            streaming={run.isPending && secondaryText.length === 0}
            text={secondaryText}
          />
        )}
      </div>
    </PageTransition>
  );
}
