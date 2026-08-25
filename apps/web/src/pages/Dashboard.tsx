import { useQuery } from '@tanstack/react-query';
import { useEffect, useState, type ReactElement } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  Badge,
  Card,
  ErrorState,
  Meter,
  PageTransition,
  SectionTitle,
  Skeleton,
  Stat,
  StatSkeleton,
  Table,
} from '@/components/ui';
import { fmt } from '@/lib/format';
import { api, openTelemetryStream } from '@/lib/api';
import { CHART, CHART_TOOLTIP } from '@/lib/chart';
import type { ClusterEventKind, ServingSnapshot } from '@/lib/types';

const WINDOWS = ['1h', '24h', '7d'] as const;
type Window = (typeof WINDOWS)[number];

const EVENT_TONE: Record<ClusterEventKind, 'ok' | 'warn' | 'danger' | 'neutral' | 'muted'> = {
  scale_up: 'neutral',
  scale_down: 'muted',
  spot_reclaim: 'warn',
  cold_start: 'muted',
  rollout_advance: 'ok',
  rollout_rollback: 'danger',
  sli_breach: 'danger',
};

function chartData(points: { at: string; value: number }[]): { t: string; v: number }[] {
  return points.map((point) => ({ t: fmt.time(point.at), v: point.value }));
}

export function Dashboard(): ReactElement {
  const [window, setWindow] = useState<Window>('24h');
  const [live, setLive] = useState<ServingSnapshot | null>(null);

  const summary = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard });
  const throughput = useQuery({
    queryKey: ['series', 'output_tokens_per_second', window],
    queryFn: () => api.series('output_tokens_per_second', window),
  });
  const ttft = useQuery({
    queryKey: ['series', 'ttft_p95_ms', window],
    queryFn: () => api.series('ttft_p95_ms', window),
  });
  const nodes = useQuery({
    queryKey: ['series', 'gpu_utilization', window],
    queryFn: () => api.series('gpu_utilization', window),
  });

  // The SSE stream is opened once and torn down on unmount. Without the
  // teardown, every navigation away leaks a connection holding a 2s timer.
  useEffect(() => openTelemetryStream(setLive), []);

  const current = live ?? summary.data?.current;

  if (summary.isError) return <ErrorState error={summary.error} />;
  if (summary.isPending) {
    return (
      <PageTransition>
        <SectionTitle eyebrow="Telemetry" title="Live dashboard" />
        <StatSkeleton count={7} />
        <div className="mt-6">
          <Skeleton rows={3} />
        </div>
      </PageTransition>
    );
  }

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Telemetry"
        title="Live dashboard"
        subtitle={summary.data.telemetry.provenance}
        action={
          <div className="border-border flex items-center gap-1 rounded-md border p-0.5">
            {WINDOWS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => {
                  setWindow(option);
                }}
                className={`rounded px-2.5 py-1 text-xs transition-colors ${
                  window === option ? 'bg-muted font-medium' : 'text-muted-foreground'
                }`}
              >
                {option}
              </button>
            ))}
          </div>
        }
      />

      {current && (
        <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-7">
          <Stat
            label="Throughput"
            value={`${fmt.compact(current.output_tokens_per_second)} tok/s`}
          />
          <Stat label="p95 TTFT" value={fmt.ms(current.ttft_p95_ms)} lowerIsBetter />
          <Stat label="ITL p50" value={`${fmt.float(current.itl_p50_ms, 1)} ms`} lowerIsBetter />
          <Stat label="Cost" value={fmt.perMtok(current.usd_per_million_tokens)} lowerIsBetter />
          <Stat label="Requests" value={`${fmt.float(current.requests_per_second, 1)}/s`} />
          <Stat label="Cache hits" value={fmt.pct(current.cache_hit_rate, 0)} />
          <Stat label="GPU pool" value={fmt.pct(current.gpu_utilization, 0)} />
        </div>
      )}

      <div className="mb-6 grid gap-4 lg:grid-cols-3">
        <Card>
          <h3 className="mb-3 text-sm font-medium">Throughput · tok/s</h3>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={chartData(throughput.data?.points ?? [])}>
              <defs>
                <linearGradient id="tp" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={CHART.accent} stopOpacity={0.22} />
                  <stop offset="100%" stopColor={CHART.accent} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 10 }} minTickGap={40} stroke={CHART.axis} />
              <YAxis tick={{ fontSize: 10 }} stroke={CHART.axis} width={44} />
              <Tooltip contentStyle={CHART_TOOLTIP} />
              <Area
                type="monotone"
                dataKey="v"
                stroke={CHART.accent}
                fill="url(#tp)"
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        </Card>

        <Card>
          <h3 className="mb-3 text-sm font-medium">p95 TTFT · ms</h3>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={chartData(ttft.data?.points ?? [])}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 10 }} minTickGap={40} stroke={CHART.axis} />
              <YAxis tick={{ fontSize: 10 }} stroke={CHART.axis} width={44} />
              <Tooltip contentStyle={CHART_TOOLTIP} />
              <Line
                type="monotone"
                dataKey="v"
                stroke={CHART.neutral}
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </Card>

        <Card>
          <h3 className="mb-3 text-sm font-medium">GPU utilisation</h3>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={chartData(nodes.data?.points ?? [])}>
              <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 10 }} minTickGap={40} stroke={CHART.axis} />
              <YAxis
                tick={{ fontSize: 10 }}
                stroke={CHART.axis}
                width={44}
                domain={[0, 1]}
                tickFormatter={(v: number) => `${String(Math.round(v * 100))}%`}
              />
              <Tooltip contentStyle={CHART_TOOLTIP} formatter={(v: number) => fmt.pct(v, 0)} />
              <Area
                type="monotone"
                dataKey="v"
                stroke={CHART.accent}
                fill={CHART.accent}
                fillOpacity={0.1}
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <SectionTitle
            title="GPU pool timeline"
            subtitle="Scale events, spot reclaims, cold starts"
          />
          <div className="max-h-96 space-y-2 overflow-y-auto pr-1" data-testid="event-timeline">
            {summary.data.events.map((event) => (
              <Card key={event.id} className="flex items-start gap-3 p-3">
                <Badge tone={EVENT_TONE[event.kind]}>{event.kind.replace(/_/g, ' ')}</Badge>
                <div className="min-w-0 flex-1">
                  <p className="text-sm">{event.message}</p>
                  <p className="text-muted-foreground mt-0.5 text-xs">
                    {fmt.time(event.at)}
                    {event.node ? ` · ${event.node}` : ''}
                  </p>
                </div>
              </Card>
            ))}
          </div>
        </div>

        <div>
          <SectionTitle title="Per-tenant breakdown" subtitle="Last 24 hours" />
          <Table headers={['Tenant', 'Requests', 'Cost', 'Student share', 'p95 TTFT']}>
            {summary.data.tenants.map((tenant) => (
              <tr key={tenant.tenant_id}>
                <td className="px-3 py-2 font-medium">{tenant.name}</td>
                <td className="px-3 py-2 font-mono tabular-nums">{fmt.compact(tenant.requests)}</td>
                <td className="px-3 py-2 font-mono tabular-nums">${tenant.cost_usd.toFixed(2)}</td>
                <td className="w-40 px-3 py-2">
                  <Meter value={tenant.student_share} tone="ok" label="student share" />
                </td>
                <td className="px-3 py-2 font-mono tabular-nums">{fmt.ms(tenant.p95_ttft_ms)}</td>
              </tr>
            ))}
          </Table>
        </div>
      </div>
    </PageTransition>
  );
}
