import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type ReactElement } from 'react';

import {
  Badge,
  type BadgeTone,
  Button,
  Card,
  Drawer,
  ErrorState,
  PageTransition,
  SectionTitle,
  Skeleton,
  Spinner,
  Table,
} from '@/components/ui';
import { fmt } from '@/lib/format';
import { api } from '@/lib/api';
import type { ModelVersion } from '@/lib/types';

type SortKey = 'created_at' | 'parity' | 'p95_ttft_ms' | 'usd_per_million_tokens';

const FAMILY_TONE: Record<string, BadgeTone> = {
  teacher: 'info',
  student: 'ok',
  quantized: 'warn',
  dpo: 'info',
};

/** `family` is a free-form string on the wire, so an unknown value must not crash. */
function familyTone(family: string): BadgeTone {
  return FAMILY_TONE[family] ?? 'muted';
}

function Detail({
  version,
  onClose,
}: {
  version: ModelVersion;
  onClose: () => void;
}): ReactElement {
  const queryClient = useQueryClient();
  const evalReport = useQuery({
    queryKey: ['eval', version.id],
    queryFn: () => api.evalReport(version.id),
    retry: false,
  });
  const promote = useMutation({
    mutationFn: () => api.promote(version.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['registry'] }),
  });

  return (
    <Drawer title={version.name} subtitle={`${version.id} · ${version.version}`} onClose={onClose}>
      <h3 className="mb-2 text-sm font-medium">Reproducibility</h3>
      <Card className="mb-5 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
        <div>
          <div className="text-muted-foreground text-xs">Git SHA</div>
          <div className="font-mono text-xs">{version.git_sha}</div>
        </div>
        <div>
          <div className="text-muted-foreground text-xs">Dataset</div>
          <div className="font-mono text-xs">{version.dataset_version}</div>
        </div>
        <div>
          <div className="text-muted-foreground text-xs">Seed</div>
          <div className="font-mono text-xs">{version.seed}</div>
        </div>
        <div>
          <div className="text-muted-foreground text-xs">Base</div>
          <div className="font-mono text-xs">{version.base_model}</div>
        </div>
        <div>
          <div className="text-muted-foreground text-xs">Created</div>
          <div className="text-xs">{fmt.date(version.created_at)}</div>
        </div>
      </Card>

      <h3 className="mb-2 text-sm font-medium">Hyperparameters</h3>
      <Card className="mb-5">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm sm:grid-cols-3">
          {Object.entries(version.hyperparameters).map(([key, value]) => (
            <div key={key}>
              <dt className="text-muted-foreground text-xs">{key.replace(/_/g, ' ')}</dt>
              <dd className="font-mono text-xs">{value}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <h3 className="mb-2 text-sm font-medium">Per-slice eval</h3>
      <Card className="mb-5">
        {evalReport.isPending && <Spinner />}
        {evalReport.isError && (
          <p className="text-muted-foreground text-sm">No eval report for this version.</p>
        )}
        {evalReport.data && (
          <div className="space-y-1 text-sm">
            {evalReport.data.slices.map((slice) => {
              const parity = slice.student_score / slice.teacher_score;
              return (
                <div key={slice.slice} className="flex items-center justify-between">
                  <span>{slice.slice.replace(/_/g, ' ')}</span>
                  <span
                    className={
                      parity < evalReport.data.gate_threshold
                        ? 'text-danger font-mono'
                        : 'text-ok font-mono'
                    }
                  >
                    {fmt.pct(parity, 1)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <h3 className="mb-2 text-sm font-medium">Promotion history</h3>
      <Card className="mb-5">
        <ol className="space-y-1.5 text-sm">
          {version.promotion_history.map((entry, index) => (
            <li key={entry} className="flex gap-2">
              <span className="text-muted-foreground font-mono text-xs">{index + 1}.</span>
              <span>{entry}</span>
            </li>
          ))}
        </ol>
      </Card>

      <Button
        variant="primary"
        disabled={promote.isPending || version.status === 'promoted'}
        onClick={() => {
          promote.mutate();
        }}
      >
        {version.status === 'promoted' ? 'Already promoted' : 'Promote to serving'}
      </Button>
      {promote.isError && <ErrorState error={promote.error} />}
      <p className="text-muted-foreground mt-2 text-xs">
        Promotion is exclusive within a family — the current holder is demoted, because two promoted
        students would be an ambiguous routing target.
      </p>
    </Drawer>
  );
}

export function Registry(): ReactElement {
  const [sort, setSort] = useState<SortKey>('created_at');
  const [selected, setSelected] = useState<ModelVersion | null>(null);
  const registry = useQuery({ queryKey: ['registry'], queryFn: api.registry });

  const sorted = useMemo(() => {
    if (!registry.data) return [];
    return [...registry.data].sort((a, b) => {
      // Latency and cost sort ascending because lower is better; quality and
      // recency sort descending. Sorting them all one way would bury the best
      // rows at the bottom half the time.
      if (sort === 'created_at') return b.created_at.localeCompare(a.created_at);
      if (sort === 'parity') return b.parity - a.parity;
      return a[sort] - b[sort];
    });
  }, [registry.data, sort]);

  if (registry.isError) return <ErrorState error={registry.error} />;
  if (registry.isPending) {
    return (
      <PageTransition>
        <SectionTitle eyebrow="Lineage" title="Model registry" />
        <Skeleton rows={6} />
      </PageTransition>
    );
  }

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Lineage"
        title="Model registry"
        subtitle={`${String(registry.data.length)} runs · every one reproducible from its git SHA, dataset version and seed`}
        action={
          <select
            value={sort}
            onChange={(event) => {
              setSort(event.target.value as SortKey);
            }}
            className="border-border bg-card rounded border px-2 py-1.5 text-sm outline-none"
          >
            <option value="created_at">Newest first</option>
            <option value="parity">Highest parity</option>
            <option value="p95_ttft_ms">Lowest TTFT</option>
            <option value="usd_per_million_tokens">Lowest cost</option>
          </select>
        }
      />

      <Table
        headers={['Model', 'Family', 'Version', 'Parity', 'p95 TTFT', '$/M tokens', 'Status', '']}
      >
        {sorted.map((version) => (
          <tr key={version.id} data-testid={`registry-${version.id}`}>
            <td className="px-3 py-2">
              <div className="font-medium">{version.name}</div>
              <div className="text-muted-foreground font-mono text-xs">{version.id}</div>
            </td>
            <td className="px-3 py-2">
              <Badge tone={familyTone(version.family)}>{version.family}</Badge>
            </td>
            <td className="px-3 py-2 font-mono text-xs">{version.version}</td>
            <td className="px-3 py-2 font-mono tabular-nums">{fmt.pct(version.parity, 1)}</td>
            <td className="px-3 py-2 font-mono tabular-nums">{fmt.ms(version.p95_ttft_ms)}</td>
            <td className="px-3 py-2 font-mono tabular-nums">
              ${version.usd_per_million_tokens.toFixed(2)}
            </td>
            <td className="px-3 py-2">
              <Badge
                tone={
                  version.status === 'promoted'
                    ? 'ok'
                    : version.status === 'candidate'
                      ? 'warn'
                      : 'muted'
                }
              >
                {version.status}
              </Badge>
            </td>
            <td className="px-3 py-2 text-right">
              <Button
                variant="ghost"
                onClick={() => {
                  setSelected(version);
                }}
              >
                Open →
              </Button>
            </td>
          </tr>
        ))}
      </Table>

      {selected && (
        <Detail
          version={selected}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}
    </PageTransition>
  );
}
