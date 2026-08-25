import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type ReactElement } from 'react';
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import {
  Badge,
  Button,
  Card,
  ErrorState,
  Meter,
  PageTransition,
  SectionTitle,
  Skeleton,
} from '@/components/ui';
import { fmt } from '@/lib/format';
import { api } from '@/lib/api';
import { CHART, CHART_TOOLTIP } from '@/lib/chart';
import { cn } from '@/lib/cn';
import type { Adapter, SliceScore } from '@/lib/types';

/**
 * Colour for a parity cell.
 *
 * Parity, not raw score: a slice where the teacher itself scores 0.6 should not
 * read as red because the student scored 0.58.
 */
function parityTone(parity: number): string {
  if (parity >= 0.97) return 'bg-ok/25 text-ok';
  if (parity >= 0.9) return 'bg-ok/15 text-ok';
  if (parity >= 0.8) return 'bg-warn/15 text-warn';
  return 'bg-danger/15 text-danger';
}

function Heatmap({ slices }: { slices: SliceScore[] }): ReactElement {
  return (
    <div className="grid gap-1" style={{ gridTemplateColumns: 'minmax(9rem,1fr) repeat(3,auto)' }}>
      <div className="text-muted-foreground text-xs font-medium uppercase tracking-wide">Slice</div>
      <div className="text-muted-foreground px-2 text-right text-xs">Student</div>
      <div className="text-muted-foreground px-2 text-right text-xs">Teacher</div>
      <div className="text-muted-foreground px-2 text-right text-xs">Parity</div>
      {slices.map((slice) => {
        const parity = slice.teacher_score === 0 ? 0 : slice.student_score / slice.teacher_score;
        return (
          <div key={slice.slice} className="contents">
            <div className="py-1 text-sm">{slice.slice.replace(/_/g, ' ')}</div>
            <div className="px-2 py-1 text-right font-mono text-sm tabular-nums">
              {slice.student_score.toFixed(3)}
            </div>
            <div className="text-muted-foreground px-2 py-1 text-right font-mono text-sm tabular-nums">
              {slice.teacher_score.toFixed(3)}
            </div>
            <div
              className={cn(
                'rounded px-2 py-1 text-right font-mono text-sm tabular-nums',
                parityTone(parity),
              )}
            >
              {fmt.pct(parity, 1)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Detail({ adapter, onClose }: { adapter: Adapter; onClose: () => void }): ReactElement {
  const queryClient = useQueryClient();
  const swap = useMutation({
    mutationFn: (status: string) => api.swapAdapter(adapter.id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['adapters'] }),
  });

  const volume = adapter.slices.map((slice) => ({
    name: slice.slice.slice(0, 10),
    requests: Math.round(adapter.requests_24h * (slice.student_score / 8)),
  }));

  return (
    <div
      className="bg-background/70 fixed inset-0 z-50 flex justify-end backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="border-border bg-card h-full w-full max-w-2xl overflow-y-auto border-l p-6"
        onClick={(event) => {
          event.stopPropagation();
        }}
        role="dialog"
        aria-label={adapter.name}
      >
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-xl font-semibold">{adapter.name}</h2>
            <p className="text-muted-foreground font-mono text-xs">{adapter.id}</p>
          </div>
          <button type="button" onClick={onClose} className="text-muted-foreground">
            ✕
          </button>
        </div>

        <div className="mb-5 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <div className="text-muted-foreground text-xs">Task</div>
            <div>{adapter.task.replace(/_/g, ' ')}</div>
          </div>
          <div>
            <div className="text-muted-foreground text-xs">Rank / alpha</div>
            <div className="font-mono">
              {adapter.rank} / {adapter.alpha}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground text-xs">Examples</div>
            <div className="font-mono">{fmt.compact(adapter.training_examples)}</div>
          </div>
          <div>
            <div className="text-muted-foreground text-xs">Trained</div>
            <div>{fmt.date(adapter.trained_at)}</div>
          </div>
        </div>

        <h3 className="mb-2 text-sm font-medium">Per-slice quality</h3>
        <Card className="mb-5">
          <Heatmap slices={adapter.slices} />
        </Card>

        <h3 className="mb-2 text-sm font-medium">Request volume by slice</h3>
        <Card className="mb-5">
          <ResponsiveContainer width="100%" height={160}>
            <BarChart data={volume}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
              <XAxis dataKey="name" tick={{ fontSize: 10 }} stroke={CHART.axis} />
              <YAxis tick={{ fontSize: 10 }} stroke={CHART.axis} width={48} />
              <Tooltip contentStyle={CHART_TOOLTIP} />
              <Bar dataKey="requests" fill={CHART.accent} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </Card>

        <h3 className="mb-2 text-sm font-medium">Hot-swap</h3>
        <div className="flex flex-wrap items-center gap-2">
          {(['active', 'shadow', 'retired'] as const).map((status) => (
            <Button
              key={status}
              variant={adapter.status === status ? 'primary' : 'default'}
              disabled={swap.isPending || adapter.status === status}
              onClick={() => {
                swap.mutate(status);
              }}
            >
              {status}
            </Button>
          ))}
          {swap.isError && <span className="text-danger text-xs">{String(swap.error)}</span>}
        </div>
        <p className="text-muted-foreground mt-2 text-xs">
          vLLM serves multiple LoRA adapters from one process, so a swap is an API call rather than
          a redeploy. Requires the <code className="font-mono">operate</code> scope.
        </p>
      </div>
    </div>
  );
}

export function Adapters(): ReactElement {
  const [selected, setSelected] = useState<Adapter | null>(null);
  const adapters = useQuery({ queryKey: ['adapters'], queryFn: api.adapters });

  if (adapters.isError) return <ErrorState error={adapters.error} />;
  if (adapters.isPending) {
    return (
      <PageTransition>
        <SectionTitle eyebrow="Serving" title="Adapters" />
        <Skeleton rows={6} />
      </PageTransition>
    );
  }

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Serving"
        title="Adapters"
        subtitle={`${String(adapters.data.length)} LoRA adapters served from one vLLM process`}
      />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {adapters.data.map((adapter) => (
          <button
            key={adapter.id}
            type="button"
            onClick={() => {
              setSelected(adapter);
            }}
            className="border-border bg-card hover:border-primary/50 rounded-lg border p-4 text-left transition-colors"
            data-testid={`adapter-${adapter.id}`}
          >
            <div className="mb-2 flex items-start justify-between gap-2">
              <h3 className="font-medium">{adapter.name}</h3>
              <Badge
                tone={
                  adapter.status === 'active'
                    ? 'ok'
                    : adapter.status === 'shadow'
                      ? 'warn'
                      : 'muted'
                }
              >
                {adapter.status}
              </Badge>
            </div>
            <p className="text-muted-foreground mb-3 text-xs">
              {adapter.task.replace(/_/g, ' ')} · rank {adapter.rank}
            </p>
            <div className="space-y-2">
              <div>
                <div className="text-muted-foreground mb-1 flex justify-between text-xs">
                  <span>Quality</span>
                  <span className="font-mono">{adapter.quality_score.toFixed(3)}</span>
                </div>
                <Meter value={adapter.quality_score} tone="ok" label="quality" showValue={false} />
              </div>
              <div>
                <div className="text-muted-foreground mb-1 flex justify-between text-xs">
                  <span>Request share</span>
                  <span className="font-mono">{fmt.pct(adapter.request_share, 1)}</span>
                </div>
                {/*
                  The meter is scaled to the largest adapter's share, so its own
                  percentage reads as a different number from the share printed
                  above it. Show one number, not two.
                */}
                <Meter
                  value={adapter.request_share}
                  max={0.2}
                  tone="info"
                  label="share"
                  showValue={false}
                />
              </div>
            </div>
            <p className="text-muted-foreground mt-3 text-xs">
              {fmt.compact(adapter.requests_24h)} requests · 24h
            </p>
          </button>
        ))}
      </div>

      {selected && (
        <Detail
          adapter={selected}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}
    </PageTransition>
  );
}
