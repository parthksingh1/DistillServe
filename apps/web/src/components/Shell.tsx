import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import { useEffect, useState, type ReactElement } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';

import { DemoAccess, DEMO_TOKEN } from '@/components/DemoAccess';
import { Badge } from '@/components/ui';
import { api, loadStoredToken } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { DeploymentMode } from '@/lib/types';

const NAV = [
  { to: '/', label: 'Overview', end: true },
  { to: '/playground', label: 'Playground' },
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/adapters', label: 'Adapters' },
  { to: '/rollouts', label: 'Rollouts' },
  { to: '/evals', label: 'Evals' },
  { to: '/traces', label: 'Traces' },
  { to: '/registry', label: 'Registry' },
] as const;

const MODE_LABEL: Record<DeploymentMode, string> = {
  hosted: 'Hosted',
  self_hosted: 'Self-hosted',
  sandbox: 'Sandbox',
};

const MODE_TONE = { hosted: 'info', self_hosted: 'ok', sandbox: 'neutral' } as const;

/**
 * The deployment-mode indicator.
 *
 * A real product control, not a disclaimer: it reports the backend pair the
 * gateway actually bound at startup, read from `/healthz`, alongside the
 * running commit.
 */
function ModeIndicator(): ReactElement {
  const health = useQuery({ queryKey: ['healthz'], queryFn: api.health, refetchInterval: 30_000 });
  const mode = health.data?.identity.mode;

  if (!mode) {
    return (
      <div className="border-border-strong bg-elevated flex items-center gap-2 rounded-md border px-2.5 py-1.5">
        <span className="skeleton size-1.5 rounded-full" />
        <span className="skeleton h-2.5 w-20" />
      </div>
    );
  }

  return (
    <Badge
      tone={MODE_TONE[mode]}
      className="px-2.5 py-1.5"
      data-testid="mode-badge"
      data-mode={mode}
    >
      <span className="live-dot" aria-hidden="true" />
      {MODE_LABEL[mode]}
      <span className="ml-0.5 font-mono opacity-60">
        {health.data?.identity.git_sha_short.slice(0, 7)}
      </span>
    </Badge>
  );
}

/**
 * ⌘K command palette.
 *
 * Navigation plus canned scenarios. The scenarios matter more than the
 * navigation: "trigger a rollback" is a one-keystroke way to show the
 * auto-rollback path to someone without hunting for the right rollout first.
 */
function CommandPalette({ onClose }: { onClose: () => void }): ReactElement {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);

  const commands = [
    ...NAV.map((item) => ({
      group: 'Navigate' as const,
      label: item.label,
      hint: item.to,
      run: () => {
        navigate(item.to);
      },
    })),
    {
      group: 'Scenario' as const,
      label: 'Trigger a rollback',
      hint: 'writes a signed audit entry',
      run: () => {
        navigate('/rollouts?scenario=rollback');
      },
    },
    {
      group: 'Scenario' as const,
      label: 'Simulate a spot reclaim',
      hint: 'GPU pool timeline',
      run: () => {
        navigate('/dashboard?scenario=spot-reclaim');
      },
    },
    {
      group: 'Scenario' as const,
      label: 'Compare teacher vs student',
      hint: 'Playground compare mode',
      run: () => {
        navigate('/playground?mode=compare');
      },
    },
    {
      group: 'Scenario' as const,
      label: 'Show the CI gate failures',
      hint: 'per-slice parity gate',
      run: () => {
        navigate('/evals?view=gate');
      },
    },
  ];

  const filtered = commands.filter((command) =>
    `${command.label} ${command.hint}`.toLowerCase().includes(query.toLowerCase()),
  );
  const active = filtered[Math.min(cursor, filtered.length - 1)];

  return (
    <div
      className="bg-background/80 fixed inset-0 z-50 flex items-start justify-center pt-[18vh] backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <motion.div
        initial={{ opacity: 0, y: -12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
        className="border-border-strong bg-surface w-full max-w-xl overflow-hidden rounded-lg border shadow-2xl"
        onClick={(event) => {
          event.stopPropagation();
        }}
        role="dialog"
        aria-label="Command palette"
      >
        <div className="border-border flex items-center gap-2.5 border-b px-4">
          <svg
            viewBox="0 0 24 24"
            className="text-muted-foreground size-4 shrink-0"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-3.5-3.5" strokeLinecap="round" />
          </svg>
          <input
            autoFocus
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setCursor(0);
            }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') {
                event.preventDefault();
                setCursor((value) => Math.min(value + 1, filtered.length - 1));
              }
              if (event.key === 'ArrowUp') {
                event.preventDefault();
                setCursor((value) => Math.max(value - 1, 0));
              }
              if (event.key === 'Enter' && active) {
                active.run();
                onClose();
              }
            }}
            placeholder="Jump to a page or run a scenario…"
            className="placeholder:text-muted-foreground w-full bg-transparent py-3.5 text-sm outline-none"
          />
          <kbd className="border-border-strong text-2xs text-muted-foreground shrink-0 rounded border px-1.5 py-0.5 font-mono">
            esc
          </kbd>
        </div>

        <ul className="max-h-80 overflow-y-auto p-1.5">
          {filtered.map((command, index) => (
            <li key={`${command.group}-${command.label}`}>
              <button
                type="button"
                onMouseEnter={() => {
                  setCursor(index);
                }}
                onClick={() => {
                  command.run();
                  onClose();
                }}
                className={cn(
                  'flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors',
                  index === cursor ? 'bg-elevated' : 'hover:bg-elevated/60',
                )}
              >
                <Badge tone={command.group === 'Scenario' ? 'info' : 'muted'}>
                  {command.group}
                </Badge>
                <span className="flex-1 truncate">{command.label}</span>
                <span className="text-2xs text-muted-foreground truncate font-mono">
                  {command.hint}
                </span>
              </button>
            </li>
          ))}
          {filtered.length === 0 ? (
            <li className="text-muted-foreground px-3 py-8 text-center text-sm">No matches</li>
          ) : null}
        </ul>
      </motion.div>
    </div>
  );
}

export function Shell(): ReactElement {
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    loadStoredToken();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
      if (event.key === 'Escape') setPaletteOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, []);

  return (
    <div className="min-h-screen">
      <header className="border-border bg-background/80 sticky top-0 z-40 border-b backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1400px] items-center gap-5 px-6 py-2.5">
          <NavLink to="/" className="flex shrink-0 items-center gap-2.5">
            <span className="border-border-strong bg-elevated text-foreground grid size-6 place-items-center rounded border font-mono text-[11px] font-semibold">
              D
            </span>
            <span className="text-sm font-semibold tracking-tight">DistillServe</span>
          </NavLink>

          <nav className="flex flex-1 items-center gap-0.5 overflow-x-auto">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={'end' in item ? item.end : false}
                className={({ isActive }) =>
                  cn(
                    'relative whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors',
                    isActive
                      ? 'text-foreground'
                      : 'text-muted-foreground hover:bg-elevated/60 hover:text-foreground',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {item.label}
                    {isActive ? (
                      <motion.span
                        layoutId="nav-active"
                        className="bg-elevated ring-border-strong absolute inset-0 -z-10 rounded-md ring-1 ring-inset"
                        transition={{ type: 'spring', stiffness: 400, damping: 32 }}
                      />
                    ) : null}
                  </>
                )}
              </NavLink>
            ))}
          </nav>

          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setPaletteOpen(true);
              }}
              className="border-border-strong bg-elevated text-2xs text-muted-foreground hover:text-foreground hidden items-center gap-1.5 rounded-md border px-2 py-1.5 transition-colors md:flex"
            >
              <span>Search</span>
              <kbd className="border-border-strong rounded border px-1 font-mono">⌘K</kbd>
            </button>
            <DemoAccess />
            <ModeIndicator />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-6 py-8">
        <Outlet />
      </main>

      <footer className="border-border/60 border-t py-6">
        <div className="text-2xs text-muted-foreground mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-3 px-6">
          <span>
            DistillServe · every number on this console comes from the gateway, not from the page
          </span>
          <span className="font-mono">demo token: {DEMO_TOKEN}</span>
        </div>
      </footer>

      <AnimatePresence>
        {paletteOpen ? (
          <CommandPalette
            onClose={() => {
              setPaletteOpen(false);
            }}
          />
        ) : null}
      </AnimatePresence>
    </div>
  );
}
