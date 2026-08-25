import { useQuery } from '@tanstack/react-query';
import { useState, type ReactElement } from 'react';
import {
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts';

import {
  Badge,
  Card,
  ErrorState,
  Meter,
  PageTransition,
  SectionTitle,
  Skeleton,
  StatSkeleton,
  Table,
} from '@/components/ui';
import { fmt } from '@/lib/format';
import { api } from '@/lib/api';
import { CHART, CHART_TOOLTIP } from '@/lib/chart';
import { cn } from '@/lib/cn';

function parityTone(parity: number, gate: number): string {
  if (parity < gate) return 'bg-danger/15 text-danger';
  if (parity >= 0.99) return 'bg-ok/25 text-ok';
  return 'bg-ok/15 text-ok';
}

export function Evals(): ReactElement {
  const reports = useQuery({ queryKey: ['evals'], queryFn: api.evals });
  const [selectedId, setSelectedId] = useState<string | null>(null);

  if (reports.isError) return <ErrorState error={reports.error} />;
  if (reports.isPending) {
    return (
      <PageTransition>
        <SectionTitle eyebrow="Quality" title="Eval reports" />
        <StatSkeleton />
        <div className="mt-6">
          <Skeleton rows={4} />
        </div>
      </PageTransition>
    );
  }

  // Default to the model actually serving traffic. The teacher is first in
  // the list and grades 100% against itself, which is a meaningless default.
  const fallback =
    reports.data.find((r) => r.model_version_id === 'mv-student-dpo-fp8-001') ??
    reports.data.find((r) => !r.model_version_id.includes('teacher')) ??
    reports.data[0];
  const report = reports.data.find((r) => r.model_version_id === selectedId) ?? fallback;
  if (!report) return <ErrorState error={new Error('No eval reports available.')} />;

  const failing = report.slices.filter(
    (slice) => slice.student_score / slice.teacher_score < report.gate_threshold,
  );

  const pareto = report.pareto.map((point) => ({
    x: point.usd_per_million_tokens,
    y: point.quality,
    z: point.p95_ttft_ms,
    model: point.model,
    frontier: point.on_frontier,
  }));

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Quality"
        title="Eval reports"
        subtitle={`${String(report.case_count)} stratified cases · judge ${report.judge_model}`}
        action={
          <select
            value={report.model_version_id}
            onChange={(event) => {
              setSelectedId(event.target.value);
            }}
            className="border-border bg-card rounded border px-2 py-1.5 text-sm outline-none"
            data-testid="model-picker"
          >
            {reports.data.map((option) => (
              <option key={option.model_version_id} value={option.model_version_id}>
                {option.model_name}
              </option>
            ))}
          </select>
        }
      />

      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Card>
          <div className="text-muted-foreground text-xs uppercase tracking-wide">
            Overall parity
          </div>
          <div className="font-mono text-2xl font-semibold">
            {fmt.pct(report.overall_parity, 1)}
          </div>
        </Card>
        <Card>
          <div className="text-muted-foreground text-xs uppercase tracking-wide">CI gate</div>
          <div className="font-mono text-2xl font-semibold">
            {fmt.pct(report.gate_threshold, 0)}
          </div>
          <div className="mt-1 text-xs">
            {failing.length === 0 ? (
              <span className="text-ok">all slices pass</span>
            ) : (
              <span className="text-danger">
                {failing.length} slice{failing.length === 1 ? '' : 's'} would fail
              </span>
            )}
          </div>
        </Card>
        <Card>
          <div className="text-muted-foreground text-xs uppercase tracking-wide">Judge drift</div>
          <div className="font-mono text-2xl font-semibold">{fmt.pct(report.judge_drift, 1)}</div>
          <div className="text-muted-foreground mt-1 text-xs">vs calibration set</div>
        </Card>
        <Card>
          <div className="text-muted-foreground text-xs uppercase tracking-wide">Dataset</div>
          <div className="font-mono text-lg font-semibold">{report.dataset_version}</div>
          <div className="text-muted-foreground mt-1 text-xs">{fmt.date(report.generated_at)}</div>
        </Card>
      </div>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <div>
          <SectionTitle
            title="Per-slice parity"
            subtitle="Red cells are below the CI gate and would block promotion"
          />
          <Card>
            <div className="space-y-1.5" data-testid="parity-heatmap">
              {report.slices.map((slice) => {
                const parity = slice.student_score / slice.teacher_score;
                return (
                  <div key={slice.slice} className="flex items-center gap-3">
                    <span className="w-40 shrink-0 text-sm">{slice.slice.replace(/_/g, ' ')}</span>
                    <div className="flex-1">
                      <Meter
                        value={parity}
                        tone={parity < report.gate_threshold ? 'danger' : 'ok'}
                        label={slice.slice}
                      />
                    </div>
                    <span
                      className={cn(
                        'w-16 rounded px-2 py-0.5 text-right font-mono text-xs tabular-nums',
                        parityTone(parity, report.gate_threshold),
                      )}
                    >
                      {fmt.pct(parity, 1)}
                    </span>
                    <span className="text-muted-foreground w-10 text-right text-xs">
                      n={slice.sample_count}
                    </span>
                  </div>
                );
              })}
            </div>
          </Card>
        </div>

        <div>
          <SectionTitle
            title="LLM-as-judge win rate"
            subtitle="Both orders judged; disagreement counts as a tie"
          />
          <Table headers={['Slice', 'Student', 'Teacher', 'Ties', 'Win rate']}>
            {report.judge.map((result) => {
              const total = result.student_wins + result.teacher_wins + result.ties;
              const rate = total === 0 ? 0 : (result.student_wins + 0.5 * result.ties) / total;
              return (
                <tr key={result.slice}>
                  <td className="px-3 py-2">{result.slice.replace(/_/g, ' ')}</td>
                  <td className="px-3 py-2 font-mono tabular-nums">{result.student_wins}</td>
                  <td className="px-3 py-2 font-mono tabular-nums">{result.teacher_wins}</td>
                  <td className="text-muted-foreground px-3 py-2 font-mono tabular-nums">
                    {result.ties}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={rate >= 0.5 ? 'ok' : rate >= 0.4 ? 'warn' : 'danger'}>
                      {fmt.pct(rate, 1)}
                    </Badge>
                  </td>
                </tr>
              );
            })}
          </Table>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <SectionTitle
            title="Adversarial suites"
            subtitle="Injection, jailbreak, distribution shift"
          />
          <div className="space-y-2">
            {report.adversarial.map((suite) => {
              const rate = suite.blocked / suite.attempts;
              return (
                <Card key={suite.suite} className="p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-sm font-medium">{suite.suite.replace(/_/g, ' ')}</span>
                    <span className="font-mono text-sm tabular-nums">
                      {suite.blocked}/{suite.attempts}
                    </span>
                  </div>
                  <Meter
                    value={rate}
                    tone={rate >= 0.95 ? 'ok' : rate >= 0.85 ? 'warn' : 'danger'}
                  />
                  {suite.passed_through > 0 && (
                    <p className="text-muted-foreground mt-1.5 text-xs">
                      {suite.passed_through} passed through — reviewed and tracked
                    </p>
                  )}
                </Card>
              );
            })}
          </div>
        </div>

        <div>
          <SectionTitle
            title="Latency / cost Pareto"
            subtitle="Filled points are on the frontier; nothing beats them on every axis"
          />
          <Card>
            <ResponsiveContainer width="100%" height={280}>
              <ScatterChart margin={{ top: 8, right: 8, bottom: 16, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} />
                <XAxis
                  type="number"
                  dataKey="x"
                  name="cost"
                  tick={{ fontSize: 10 }}
                  stroke={CHART.axis}
                  label={{
                    value: '$/M tokens',
                    position: 'insideBottom',
                    offset: -8,
                    fontSize: 11,
                  }}
                />
                <YAxis
                  type="number"
                  dataKey="y"
                  name="quality"
                  domain={[0.85, 1.01]}
                  tick={{ fontSize: 10 }}
                  stroke={CHART.axis}
                  width={48}
                />
                <ZAxis type="number" dataKey="z" range={[40, 240]} name="p95 TTFT" />
                <Tooltip
                  cursor={{ strokeDasharray: '3 3' }}
                  contentStyle={CHART_TOOLTIP}
                  formatter={(value: number, name: string) => [value, name]}
                />
                <Scatter data={pareto}>
                  {pareto.map((point) => (
                    <Cell key={point.model} fill={point.frontier ? CHART.accent : CHART.faint} />
                  ))}
                </Scatter>
              </ScatterChart>
            </ResponsiveContainer>
          </Card>
        </div>
      </div>
    </PageTransition>
  );
}
