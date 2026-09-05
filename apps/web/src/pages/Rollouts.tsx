import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ReactElement } from 'react';
import { useSearchParams } from 'react-router-dom';

import {
  Badge,
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
import { cn } from '@/lib/cn';
import { STAGE_TRAFFIC, type Rollout, type RolloutStage, type SLIStatus } from '@/lib/types';

const STAGES: RolloutStage[] = ['shadow', 'canary_10', 'canary_50', 'full'];

const SLI_TONE: Record<SLIStatus, 'ok' | 'warn' | 'danger'> = {
  healthy: 'ok',
  at_risk: 'warn',
  breached: 'danger',
};

/**
 * The state machine, drawn.
 *
 * `rolled_back` sits off the line rather than at the end of it, because it is
 * reachable from every stage — drawing it inline would imply it is the stage
 * after `full`.
 */
function StateMachine({ stage }: { stage: RolloutStage }): ReactElement {
  const rolledBack = stage === 'rolled_back';
  const currentIndex = STAGES.indexOf(stage);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-1">
        {STAGES.map((step, index) => {
          const reached = !rolledBack && index <= currentIndex;
          const active = !rolledBack && index === currentIndex;
          return (
            <div key={step} className="flex flex-1 items-center gap-1">
              <div
                className={cn(
                  'flex-1 rounded-md border px-2 py-2 text-center text-xs transition-colors',
                  active && 'border-primary bg-primary/15 text-primary font-medium',
                  reached && !active && 'border-ok/40 bg-ok/10 text-ok',
                  !reached && 'border-border text-muted-foreground',
                )}
                data-testid={`stage-${step}`}
              >
                {step.replace(/_/g, ' ')}
                <div className="mt-0.5 font-mono text-[10px] opacity-70">
                  {STAGE_TRAFFIC[step]}%
                </div>
              </div>
              {index < STAGES.length - 1 && <span className="text-muted-foreground">→</span>}
            </div>
          );
        })}
      </div>
      <div className="text-muted-foreground flex items-center gap-2 text-xs">
        <span>↳ rollback from any stage:</span>
        <Badge tone={rolledBack ? 'danger' : 'muted'}>rolled back</Badge>
      </div>
    </div>
  );
}

function Detail({ id, onClose }: { id: string; onClose: () => void }): ReactElement {
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ['rollout', id], queryFn: () => api.rollout(id) });
  const [reason, setReason] = useState('Manual rollback from the console');

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ['rollout', id] });
    await queryClient.invalidateQueries({ queryKey: ['rollouts'] });
  };

  const advance = useMutation({ mutationFn: () => api.advanceRollout(id), onSuccess: invalidate });
  const rollback = useMutation({
    mutationFn: () => api.rollbackRollout(id, reason),
    onSuccess: invalidate,
  });

  return (
    <Drawer
      title={detail.data?.rollout.name ?? 'Rollout'}
      subtitle={detail.data?.rollout.id}
      onClose={onClose}
    >
      {detail.isPending && <Spinner />}
      {detail.isError && <ErrorState error={detail.error} />}
      {detail.data && (
        <>
          <Card className="mb-5">
            <StateMachine stage={detail.data.rollout.stage} />
          </Card>

          <h3 className="mb-2 text-sm font-medium">Service-level indicators</h3>
          <div className="mb-5 space-y-2">
            {detail.data.rollout.slis.map((sli) => (
              <Card key={sli.name} className="flex items-center justify-between p-3">
                <div>
                  <div className="text-sm font-medium">{sli.name.replace(/_/g, ' ')}</div>
                  <div className="text-muted-foreground text-xs">
                    {sli.comparison === 'gte' ? 'must stay above' : 'must stay below'}{' '}
                    <span className="font-mono">
                      {sli.threshold} {sli.unit}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span className="font-mono text-sm tabular-nums">
                    {sli.value} {sli.unit}
                  </span>
                  <Badge tone={SLI_TONE[sli.status]}>{sli.status.replace(/_/g, ' ')}</Badge>
                </div>
              </Card>
            ))}
          </div>

          <div className="mb-5 flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              disabled={
                advance.isPending ||
                detail.data.rollout.stage === 'full' ||
                detail.data.rollout.stage === 'rolled_back' ||
                detail.data.rollout.slis.some((sli) => sli.status === 'breached')
              }
              onClick={() => {
                advance.mutate();
              }}
              data-testid="advance-rollout"
            >
              Advance stage
            </Button>
            <Button
              variant="danger"
              disabled={rollback.isPending || detail.data.rollout.stage === 'rolled_back'}
              onClick={() => {
                rollback.mutate();
              }}
              data-testid="trigger-rollback"
            >
              Trigger rollback
            </Button>
            <input
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
              }}
              className="border-border bg-background flex-1 rounded border px-2 py-1.5 text-xs outline-none"
              placeholder="Rollback reason (recorded verbatim)"
            />
          </div>
          {advance.isError && <ErrorState error={advance.error} />}

          <h3 className="mb-2 flex items-center gap-2 text-sm font-medium">
            Audit trail
            {detail.data.audit.length === 0 ? (
              <Badge tone="muted">no entries</Badge>
            ) : (
              <Badge tone={detail.data.audit_verified ? 'ok' : 'danger'}>
                {detail.data.audit_verified ? 'chain verified' : 'chain broken'}
              </Badge>
            )}
          </h3>
          <p className="text-muted-foreground mb-2 text-xs">{detail.data.audit_detail}</p>
          <div className="space-y-2" data-testid="audit-trail">
            {detail.data.audit.map((entry) => (
              <Card key={entry.id} className="p-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium">{entry.kind.replace(/_/g, ' ')}</span>
                  <span className="text-muted-foreground text-xs">{fmt.time(entry.at)}</span>
                </div>
                <p className="text-muted-foreground mt-0.5 text-xs">
                  {entry.actor} · {entry.reason}
                </p>
                <p className="text-muted-foreground mt-1 truncate font-mono text-[10px]">
                  sig {entry.signature.slice(0, 32)}…
                </p>
              </Card>
            ))}
            {detail.data.audit.length === 0 && (
              <p className="text-muted-foreground text-sm">No entries yet.</p>
            )}
          </div>
        </>
      )}
    </Drawer>
  );
}

export function Rollouts(): ReactElement {
  const [params] = useSearchParams();
  const [selected, setSelected] = useState<string | null>(null);
  const rollouts = useQuery({ queryKey: ['rollouts'], queryFn: api.rollouts });

  // The ⌘K "trigger a rollback" scenario deep-links here and opens the first
  // rollout that can actually be rolled back.
  useEffect(() => {
    if (params.get('scenario') === 'rollback' && rollouts.data && !selected) {
      const candidate = rollouts.data.find((r) => r.stage !== 'rolled_back');
      if (candidate) setSelected(candidate.id);
    }
  }, [params, rollouts.data, selected]);

  if (rollouts.isError) return <ErrorState error={rollouts.error} />;
  if (rollouts.isPending) {
    return (
      <PageTransition>
        <SectionTitle eyebrow="Delivery" title="Canary control" />
        <Skeleton rows={4} />
      </PageTransition>
    );
  }

  const tone = (rollout: Rollout) => {
    if (rollout.stage === 'rolled_back') return 'danger' as const;
    if (rollout.stage === 'full') return 'ok' as const;
    return 'info' as const;
  };

  return (
    <PageTransition>
      <SectionTitle
        eyebrow="Delivery"
        title="Canary control"
        subtitle="shadow → canary_10 → canary_50 → full, with automatic rollback on an SLI breach"
      />

      <Table headers={['Rollout', 'Candidate', 'Stage', 'Traffic', 'SLIs', 'Updated', '']}>
        {rollouts.data.map((rollout) => {
          const breached = rollout.slis.filter((s) => s.status === 'breached').length;
          return (
            <tr key={rollout.id} data-testid={`rollout-${rollout.id}`}>
              <td className="px-3 py-2">
                <div className="font-medium">{rollout.name}</div>
                <div className="text-muted-foreground font-mono text-xs">{rollout.id}</div>
              </td>
              <td className="px-3 py-2 font-mono text-xs">{rollout.candidate_model}</td>
              <td className="px-3 py-2">
                <Badge tone={tone(rollout)}>{rollout.stage.replace(/_/g, ' ')}</Badge>
              </td>
              <td className="px-3 py-2 font-mono tabular-nums">{rollout.traffic_percent}%</td>
              <td className="px-3 py-2">
                {breached > 0 ? (
                  <Badge tone="danger">{breached} breached</Badge>
                ) : (
                  <Badge tone="ok">healthy</Badge>
                )}
              </td>
              <td className="text-muted-foreground px-3 py-2 text-xs">
                {fmt.date(rollout.updated_at)}
              </td>
              <td className="px-3 py-2 text-right">
                <Button
                  variant="ghost"
                  onClick={() => {
                    setSelected(rollout.id);
                  }}
                >
                  Open →
                </Button>
              </td>
            </tr>
          );
        })}
      </Table>

      {selected && (
        <Detail
          id={selected}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}
    </PageTransition>
  );
}
