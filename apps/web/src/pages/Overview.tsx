import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { useState, type ReactElement } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Card, Drawer, PageTransition, SectionTitle, Stat } from '@/components/ui';
import { api } from '@/lib/api';
import { fmt } from '@/lib/format';

/**
 * The landing page: what this is, what it achieved, and how it is built.
 *
 * Headline numbers come from the backend rather than being written into the
 * markup, so the page and the dashboards can never disagree — which is the
 * single most common way a project's front page becomes a lie.
 */

interface Component {
  id: string;
  title: string;
  summary: string;
  detail: string;
  file: string;
  code: string;
}

const ARCHITECTURE: Component[] = [
  {
    id: 'gateway',
    title: 'Gateway',
    summary: 'OpenAI-compatible entry point',
    file: 'apps/gateway/pipeline.py',
    detail:
      'One pipeline handles streaming and buffered requests, so they cannot diverge in routing, cost or tracing. The stage order below is literally the order executed.',
    code: `await self._enforce_rate_limit(span, tenant_id, trace_id, rpm)
self._screen_prompt(span, prompt, trace_id)
redaction = self._redact(span, prompt)
route = self._router.route(prompt=redaction.text, ...)
cached = await self._lookup_cache(span, redaction.text, tenant, route.model)`,
  },
  {
    id: 'router',
    title: 'Router',
    summary: 'Rules + embedding fallback',
    file: 'routing/router.py',
    detail:
      'Escalates on uncertainty. Routing a hard prompt to the student costs quality; routing an easy one to the teacher costs cents.',
    code: `if classification.task not in self._config.student_capable_tasks:
    return self._teacher(...)   # outside the distilled scope
if classification.confidence < self._config.threshold:
    return self._teacher(...)   # below escalation threshold
return self._student(...)`,
  },
  {
    id: 'cache',
    title: 'Semantic cache',
    summary: 'Redis + embeddings',
    file: 'core/cache.py',
    detail:
      'Keyed by tenant AND model. Tenant isolation is a security boundary; model isolation stops the student answering a teacher-routed request, which would destroy the quality comparison.',
    code: `@staticmethod
def namespace(tenant_id: str, model: str) -> str:
    return f"{tenant_id}:{model}"`,
  },
  {
    id: 'guardrails',
    title: 'Guardrails',
    summary: 'Injection screen + PII redaction',
    file: 'guardrails/pii.py',
    detail:
      'Redaction is reversible within a request: placeholders go upstream, real values return in the output. Irreversible masking is why most redaction layers get switched off.',
    code: `result = redactor.redact(prompt)      # "<EMAIL_1>" goes upstream
answer = result.rehydrate(model_out)  # real value comes back`,
  },
  {
    id: 'backends',
    title: 'Backends',
    summary: 'Hosted · self-hosted · sandbox',
    file: 'bootstrap.py',
    detail:
      'Three implementations of one protocol. This is the only file in the codebase allowed to branch on the deployment mode; everything above it is shared.',
    code: `if settings.mode is DeploymentMode.SELF_HOSTED:
    return SelfHostedInferenceBackend(settings.vllm_endpoint)
if settings.mode is DeploymentMode.SANDBOX:
    return SandboxInferenceBackend(hosted, SandboxTelemetry(store))
return hosted`,
  },
  {
    id: 'rollouts',
    title: 'Rollout controller',
    summary: 'shadow → canary → full',
    file: 'platform/rollouts.py',
    detail:
      'A reconciliation loop, not a workflow: idempotent and interruptible. Advance is refused while an SLI is breached.',
    code: `breached = self.breached(rollout.slis)
if breached:
    raise RolloutTransitionError(
        f"Cannot advance: SLI(s) breached: {', '.join(breached)}.")`,
  },
  {
    id: 'audit',
    title: 'Signed audit log',
    summary: 'Hash-chained SQLite',
    file: 'platform/rollouts.py',
    detail:
      "Each signature covers its own row and the previous row's signature, so a tampered or deleted entry breaks the chain. That is the difference between an audit log and a log.",
    code: `def _sign(self, payload, previous):
    chained = f"{previous or ''}|{json.dumps(payload, sort_keys=True)}"
    return hmac.new(self._secret, chained.encode(), sha256).hexdigest()`,
  },
  {
    id: 'evals',
    title: 'Eval harness',
    summary: 'LLM-as-judge, debiased',
    file: 'platform/evals.py',
    detail:
      'Every pairwise comparison runs in both orders. Disagreement means the judge is responding to position, not quality, and is recorded as a tie.',
    code: `first, second = await asyncio.gather(
    self._ask(JUDGE, forward), self._ask(JUDGE, reverse))
consistent = forward_winner == reverse_winner
winner = forward_winner if consistent else "tie"`,
  },
];

const DECISIONS = [
  {
    chose: 'Generated reference dataset',
    rejected: 'Hand-written fixtures',
    because:
      'The numbers are interdependent — throughput sets cost, utilisation sets how much of it is idle. Hand-edited fixtures drift apart and produce a dashboard that contradicts itself.',
  },
  {
    chose: 'LiteLLM as a thin adapter',
    rejected: 'LangChain',
    because:
      'One normalised streaming chunk shape across four providers, without inheriting a framework’s control flow. DistillServe keeps routing, retries, cost and tracing.',
  },
  {
    chose: 'OTel GenAI → Langfuse over OTLP',
    rejected: 'The Langfuse SDK',
    because:
      'Vendor-neutral spans. The same instrumentation fans out to a collector, Tempo or Honeycomb by changing one endpoint, and the attribute names are a public spec.',
  },
  {
    chose: 'Token bucket rate limiting',
    rejected: 'Fixed window',
    because:
      'A fixed window lets a tenant spend its whole minute in the last second and again in the first — a 2× burst against the upstream provider exactly when you need to stay under its limit.',
  },
  {
    chose: 'Reversible PII redaction',
    rejected: 'Irreversible masking',
    because:
      'A model asked to "write a reply to this customer" must still produce a usable reply. Irreversible masking breaks the product, which is why those layers get switched off.',
  },
  {
    chose: 'Zustand + TanStack Query',
    rejected: 'Redux Toolkit',
    because:
      'Almost all state here is server state, which TanStack Query owns. What remains is a handful of UI toggles; RTK’s boilerplate buys nothing at that size.',
  },
  {
    chose: 'SQLite for registry and audit',
    rejected: 'Postgres',
    because:
      'Small, read-mostly data whose value is being present. A registry you must provision a database for is a registry that does not exist in month one.',
  },
  {
    chose: 'Escalate on router uncertainty',
    rejected: 'Default to the cheap model',
    because:
      'A mis-routed hard prompt costs quality, which is the thing distillation exists to protect. A mis-routed easy prompt costs a fraction of a cent.',
  },
];

export function Overview(): ReactElement {
  const [selected, setSelected] = useState<Component | null>(null);
  const dashboard = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard });
  const registry = useQuery({ queryKey: ['registry'], queryFn: api.registry });

  const current = dashboard.data?.current;
  // The promoted *student*, not the promoted teacher. The teacher scores 1.000
  // parity against itself by definition, and showing that as the headline
  // would advertise a number that means nothing.
  const promoted = registry.data?.find(
    (version) => version.status === 'promoted' && version.family !== 'teacher',
  );
  const loading = dashboard.isPending;

  return (
    <PageTransition>
      {/* Hero */}
      <section className="relative -mx-6 -mt-8 mb-12 overflow-hidden px-6 pb-12 pt-16">
        <div className="grid-lines pointer-events-none absolute inset-0 -z-10" aria-hidden="true" />

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        >
          <Badge tone="neutral" className="mb-5 px-3 py-1">
            <span className="live-dot" aria-hidden="true" />
            Task-specific distillation · vLLM serving · canary rollout
          </Badge>

          <h1 className="max-w-4xl text-4xl font-semibold leading-[1.08] tracking-tight md:text-5xl">
            Route the easy work to a distilled 8B student.
            <span className="text-muted-foreground"> Keep the frontier model for the rest.</span>
          </h1>

          <p className="text-muted-foreground mt-5 max-w-2xl text-base leading-relaxed">
            DistillServe routes production traffic between hosted frontier models and LoRA-distilled
            open-weights students, serves multi-adapter vLLM, and gates every promotion behind a
            per-slice quality eval with automatic rollback.
          </p>

          <div className="mt-7 flex flex-wrap items-center gap-2.5">
            <Link
              to="/playground"
              className="border-primary/40 bg-primary/15 text-primary hover:bg-primary/25 inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition-colors"
            >
              Open the Playground
              <span aria-hidden="true">→</span>
            </Link>
            <Link
              to="/dashboard"
              className="border-border-strong bg-elevated hover:bg-muted inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition-colors"
            >
              Live dashboard
            </Link>
            <span className="text-muted-foreground ml-1 text-xs">
              or press{' '}
              <kbd className="border-border-strong text-2xs rounded border px-1.5 py-0.5 font-mono">
                ⌘K
              </kbd>
            </span>
          </div>
        </motion.div>
      </section>

      {/* Headline numbers */}
      <div className="mb-14 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Quality parity"
          value={promoted ? fmt.pct(promoted.parity, 1) : '—'}
          hint="per-slice, vs teacher"
          accent
          loading={registry.isPending}
        />
        <Stat
          label="Serving cost"
          value={current ? fmt.perMtok(current.usd_per_million_tokens) : '—'}
          hint="from $2.80/M frontier"
          trend="down"
          lowerIsBetter
          loading={loading}
        />
        <Stat
          label="Throughput"
          value={
            current
              ? `${fmt.compact(current.output_tokens_per_second / Math.max(1, current.active_nodes))} tok/s`
              : '—'
          }
          hint="per H100 · FP8 + EAGLE-3"
          trend="up"
          loading={loading}
        />
        <Stat
          label="p95 TTFT"
          value={current ? fmt.ms(current.ttft_p95_ms) : '—'}
          hint="continuous batching"
          trend="down"
          lowerIsBetter
          loading={loading}
        />
      </div>

      {/* Architecture */}
      <section className="mb-14">
        <SectionTitle
          eyebrow="Architecture"
          title="Eight components, one request path"
          subtitle="Click any component to see the code that implements it."
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {ARCHITECTURE.map((component, index) => (
            <motion.button
              key={component.id}
              type="button"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, delay: index * 0.03 }}
              onClick={() => {
                setSelected(component);
              }}
              className="surface-card surface-card-hover group p-4 text-left"
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="text-2xs text-muted-foreground font-mono">
                  {String(index + 1).padStart(2, '0')}
                </span>
                <span className="text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">
                  →
                </span>
              </div>
              <h3 className="text-sm font-semibold">{component.title}</h3>
              <p className="text-muted-foreground mt-1 text-xs">{component.summary}</p>
            </motion.button>
          ))}
        </div>
      </section>

      {selected ? (
        <Drawer
          title={selected.title}
          subtitle={selected.file}
          onClose={() => {
            setSelected(null);
          }}
        >
          <p className="text-muted-foreground mb-5 text-sm leading-relaxed">{selected.detail}</p>
          <pre className="border-border bg-background overflow-x-auto rounded-lg border p-4 font-mono text-xs leading-relaxed">
            <code>{selected.code}</code>
          </pre>
        </Drawer>
      ) : null}

      {/* Design decisions */}
      <section className="mb-14">
        <SectionTitle
          eyebrow="Design decisions"
          title="Chose X, rejected Y, because Z"
          subtitle="Eight of them. The reasoning matters more than the choice."
        />
        <div className="grid gap-3 md:grid-cols-2">
          {DECISIONS.map((decision) => (
            <Card key={decision.chose} interactive>
              <div className="mb-2.5 flex flex-wrap items-center gap-2">
                <Badge tone="ok">{decision.chose}</Badge>
                <span className="text-2xs text-muted-foreground">over</span>
                <Badge tone="muted">{decision.rejected}</Badge>
              </div>
              <p className="text-muted-foreground text-sm leading-relaxed">{decision.because}</p>
            </Card>
          ))}
        </div>
      </section>

      {/* Where to start */}
      <section>
        <SectionTitle eyebrow="Get started" title="Three places worth looking" />
        <div className="grid gap-3 sm:grid-cols-3">
          {[
            {
              to: '/playground',
              title: 'Playground',
              body: 'Send a prompt. Watch it route, stream and get priced in real time.',
            },
            {
              to: '/rollouts',
              title: 'Canary control',
              body: 'Advance a stage, or trigger a rollback and read the signed audit entry.',
            },
            {
              to: '/evals',
              title: 'Eval reports',
              body: 'Per-slice parity, judge win rate, and which slices fail the CI gate.',
            },
          ].map((item) => (
            <Link key={item.to} to={item.to} className="surface-card surface-card-hover group p-5">
              <h3 className="flex items-center gap-1.5 text-sm font-semibold">
                {item.title}
                <span className="text-muted-foreground transition-transform group-hover:translate-x-0.5">
                  →
                </span>
              </h3>
              <p className="text-muted-foreground mt-1.5 text-sm">{item.body}</p>
            </Link>
          ))}
        </div>
      </section>
    </PageTransition>
  );
}
