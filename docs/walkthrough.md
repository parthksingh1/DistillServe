# Two-minute walkthrough

A click-through of DistillServe for someone seeing it for the first time. Every
number on every page comes from the backend — there is no hard-coded content in
the console.

---

## 0. Before you start (10 seconds)

Look at the top-right of the nav. The **mode indicator** reports which backend
pair the gateway bound at startup — `hosted`, `self-hosted` or `sandbox` —
followed by the running commit. It is read from `/healthz`, so its presence
means the console has actually reached the gateway.

Press **⌘K** (Ctrl-K). The command palette jumps between pages and runs canned
scenarios: _trigger a rollback_, _simulate a spot reclaim_, _compare teacher vs
student_.

---

## 1. Overview (20 seconds)

The four headline numbers — parity, cost, throughput, p95 TTFT — are fetched,
not written into the page. That is deliberate: a front page that hard-codes its
own results is the most common way a project's claims and its dashboards drift
apart.

Click any **architecture component** to open a side panel with the real code
that implements it. The router panel shows the actual escalation branch; the
audit panel shows the actual HMAC chaining.

Scroll to **design decisions** — eight of them, each with what was rejected and
why.

---

## 2. Playground (40 seconds)

Pick the **"Summarize a support thread"** sample and press **Send**.

Watch the response stream. Below it:

- **route** — `student`, with the reason: _"task 'summarization' is distilled
  and confidence 0.90 clears the threshold"_.
- **TTFT** — measured in the browser, so it includes the network, which is what
  a user actually experiences.
- **cost** — priced from `config/prices.yaml`, to six decimal places because a
  single completion costs fractions of a cent.

Now pick **"Multi-step reasoning"** and send it. The route flips to `teacher`,
with a different reason: reasoning is outside the student's distilled scope. The
router escalates on uncertainty because a mis-routed hard prompt costs quality,
while a mis-routed easy one costs a fraction of a cent.

Tick **Compare mode** and send again. Both models run _in parallel_ — sequential
calls would make the second side's latency include the first's — and the cost
and TTFT deltas appear above the panes.

---

## 3. Traces (20 seconds)

Every Playground call landed here. Click one to expand the **waterfall**:

```
Prompt-injection check → PII redaction → Semantic cache lookup
  → Router decision → LLM call → Cost accounting
```

That is not a diagram of the pipeline; it is the pipeline's real execution
order, taken from the same list the gateway executes. The LLM call visually
dominates because everything else is microseconds by comparison.

Press **Replay** against the other model to diff the outputs.

---

## 4. Live Dashboard (20 seconds)

Tiles update over SSE. Note the line under the title: it names the telemetry
source and the **dataset digest**, so a screenshot can be pinned to an exact
data revision.

The **GPU pool timeline** shows scale-ups, spot reclaims and cold starts. Each
spot reclaim is followed by a replacement scale-up and a cold start — that
sequence is what makes cold-start time worth caring about, and it is why
aggressive scale-down is expensive.

Switch the window between **1h / 24h / 7d**.

---

## 5. Canary Control (30 seconds)

The rollout list shows four rollouts. Open the one that **rolled back** —
`rol-2026-07-int4`. Its quality-parity SLI reads 0.891 against a 0.95 gate, and
the audit trail records the automatic rollback.

Open a healthy one and press **Advance stage**. Then look at the breached one
and try to advance it: the gateway refuses with a 409. Advancing a rollout whose
quality gate is failing is the most expensive mistake this controller could
permit, so it is blocked in code rather than left to judgement.

Press **Trigger rollback** on any active rollout. The audit trail gains a new
entry with a signature, and the badge still reads **chain verified** — each
signature covers its own row _and the previous row's signature_, so a tampered
or deleted entry breaks the chain.

---

## 6. Eval Reports (20 seconds)

Pick a model version. The **per-slice parity** bars show where the student holds
up and where it does not — reasoning and code generation are the weak slices,
which is exactly why the router does not send them to the student.

Red cells are below the **CI gate** and would block promotion.

The **judge win rate** counts ties as half a win, and every comparison was run
in both orders: judges prefer whichever answer they see first, often by 5–10
points, so a verdict only counts if it survives the swap.

The **Pareto chart** plots quality against cost, with point size as p95 TTFT.
Filled points are on the frontier — nothing beats them on every axis.

---

## 7. Adapters and Registry (20 seconds)

**Adapters**: twelve LoRA adapters served from one vLLM process. Open one for
its per-slice heatmap. **Hot-swap** between active, shadow and retired — vLLM
selects an adapter by name, so this is an API call, not a redeploy.

**Registry**: fifteen runs. Every one carries its git SHA, dataset version and
seed, so it is reproducible. Open one to see hyperparameters and promotion
history. Promotion is exclusive within a family, because two promoted students
would be an ambiguous routing target.

---

## What to look at in the code

| If they ask about… | Point at                                                            |
| ------------------ | ------------------------------------------------------------------- |
| Mode switching     | `bootstrap.py` — the only branch on mode in the codebase            |
| Pipeline ordering  | `pipeline.py::_run` — the stage order and why each position matters |
| Cost model         | `core/pricing.py` — utilisation in the denominator                  |
| Audit integrity    | `platform/rollouts.py::AuditLog._sign`                              |
| Judge debiasing    | `platform/evals.py::LLMJudge.pairwise`                              |
| Dataset provenance | `data/reference/generator.yaml` + `sources/catalog.yaml`            |
