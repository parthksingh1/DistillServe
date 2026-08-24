import { useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { useState, type ReactElement } from 'react';

import { Badge, Button } from '@/components/ui';
import { api, setAuthToken } from '@/lib/api';
import { cn } from '@/lib/cn';

/**
 * The published demo credential.
 *
 * Shown in the UI on purpose. The demo tenant holds `read` and `infer` only,
 * is rate- and budget-capped, and cannot change any platform state — so there
 * is nothing to protect, and hiding it would only make the console harder to
 * hand to someone.
 */
export const DEMO_TOKEN = 'distillserve-demo';

const SCOPE_COPY: Record<string, string> = {
  read: 'Dashboards, traces, evals, adapters, registry',
  infer: 'Playground, Compare mode, eval harness',
  operate: 'Advance/roll back rollouts, hot-swap adapters, promote models',
  admin: 'Tenant and platform configuration',
};

const ALL_SCOPES = ['read', 'infer', 'operate', 'admin'] as const;

/**
 * The identity control in the nav.
 *
 * Opens a panel that both explains the scope model and hands over the demo
 * credential in one click. Someone shown this console should not have to be
 * told separately how to sign in.
 */
export function DemoAccess(): ReactElement {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState('');
  const [copied, setCopied] = useState(false);

  const me = useQuery({ queryKey: ['me'], queryFn: api.me, retry: false });
  const scopes = me.data?.scopes ?? [];

  const apply = (token: string | null) => {
    setAuthToken(token);
    void queryClient.invalidateQueries();
    setOpen(false);
  };

  const copy = () => {
    void navigator.clipboard.writeText(DEMO_TOKEN).then(() => {
      setCopied(true);
      setTimeout(() => {
        setCopied(false);
      }, 1600);
    });
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => {
          setOpen((value) => !value);
        }}
        data-testid="identity-button"
        className="border-border-strong bg-elevated hover:border-muted-foreground/30 flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs transition-colors"
      >
        <span
          className={cn(
            'size-1.5 rounded-full',
            me.data?.authenticated ? 'bg-ok' : 'bg-muted-foreground',
          )}
        />
        <span className="font-medium">{me.data?.name ?? 'Sign in'}</span>
        {me.data ? (
          <span className="text-2xs text-muted-foreground font-mono">{me.data.role}</span>
        ) : null}
      </button>

      {open ? (
        <>
          <div
            className="fixed inset-0 z-40"
            onClick={() => {
              setOpen(false);
            }}
            role="presentation"
          />
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ duration: 0.15 }}
            className="surface-card absolute right-0 z-50 mt-2 w-80 p-4"
            role="dialog"
            aria-label="Access"
          >
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold">Access</h3>
              {me.data ? (
                <Badge tone={me.data.authenticated ? 'ok' : 'muted'}>{me.data.tenant_id}</Badge>
              ) : null}
            </div>

            <div className="mb-3 space-y-1.5">
              {ALL_SCOPES.map((scope) => {
                const held = scopes.includes(scope);
                return (
                  <div key={scope} className="text-2xs flex items-start gap-2">
                    <span className={held ? 'text-ok' : 'text-muted-foreground/50'}>
                      {held ? '✓' : '✕'}
                    </span>
                    <span
                      className={cn(
                        'font-mono',
                        held ? 'text-foreground' : 'text-muted-foreground/60',
                      )}
                    >
                      {scope}
                    </span>
                    <span className="text-muted-foreground flex-1">{SCOPE_COPY[scope]}</span>
                  </div>
                );
              })}
            </div>

            <div className="border-primary/25 bg-primary/5 rounded-md border p-3">
              <div className="text-2xs text-primary mb-1.5 font-semibold uppercase tracking-[0.1em]">
                Demo credential
              </div>
              <p className="text-2xs text-muted-foreground mb-2 leading-relaxed">
                Read and infer only. Drive the whole product; change nothing.
              </p>
              <div className="flex items-center gap-1.5">
                <code className="bg-background text-2xs flex-1 truncate rounded px-2 py-1 font-mono">
                  {DEMO_TOKEN}
                </code>
                <Button size="sm" variant="ghost" onClick={copy}>
                  {copied ? '✓' : 'Copy'}
                </Button>
              </div>
              <Button
                size="sm"
                variant="primary"
                className="mt-2 w-full"
                data-testid="use-demo-token"
                onClick={() => {
                  apply(DEMO_TOKEN);
                }}
              >
                Sign in as demo
              </Button>
            </div>

            <form
              className="mt-3 flex items-center gap-1.5"
              onSubmit={(event) => {
                event.preventDefault();
                apply(custom || null);
              }}
            >
              <input
                value={custom}
                onChange={(event) => {
                  setCustom(event.target.value);
                }}
                placeholder="Your own token"
                className="border-border-strong bg-background text-2xs focus:border-primary/50 min-w-0 flex-1 rounded-md border px-2 py-1 font-mono outline-none"
              />
              <Button size="sm" type="submit">
                Use
              </Button>
            </form>

            {me.data?.authenticated ? (
              <button
                type="button"
                onClick={() => {
                  apply(null);
                }}
                className="text-2xs text-muted-foreground hover:text-foreground mt-2 w-full"
              >
                Sign out
              </button>
            ) : null}
          </motion.div>
        </>
      ) : null}
    </div>
  );
}
