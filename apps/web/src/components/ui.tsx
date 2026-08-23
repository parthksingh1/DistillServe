import { motion } from 'framer-motion';
import type { ReactElement, ReactNode } from 'react';

import { cn } from '@/lib/cn';

/**
 * The console's primitive vocabulary.
 *
 * Every page composes from these rather than styling ad hoc, so a token change
 * propagates everywhere and eight dense pages stay recognisably one product.
 * Nothing here fetches — these are presentation only.
 */

export function Card({
  children,
  className,
  interactive,
  ...rest
}: {
  children: ReactNode;
  className?: string;
  interactive?: boolean;
} & Record<string, unknown>): ReactElement {
  return (
    <div
      className={cn('surface-card p-5', interactive && 'surface-card-hover', className)}
      {...rest}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  title,
  subtitle,
  action,
  eyebrow,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  eyebrow?: string;
}): ReactElement {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow ? (
          <div className="text-2xs text-muted-foreground mb-1.5 font-semibold uppercase tracking-[0.14em]">
            {eyebrow}
          </div>
        ) : null}
        <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
        {subtitle ? <p className="text-muted-foreground mt-1 text-sm">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}

export interface StatProps {
  label: string;
  value: string;
  hint?: string;
  trend?: 'up' | 'down' | 'flat';
  /** Marks a number whose direction of "good" is inverted (latency, cost). */
  lowerIsBetter?: boolean;
  accent?: boolean;
  loading?: boolean;
}

/**
 * A single headline number.
 *
 * Trend colour respects `lowerIsBetter`, because a rising cost and a rising
 * throughput are not both green — getting that backwards is the fastest way to
 * make a dashboard actively misleading.
 */
export function Stat({
  label,
  value,
  hint,
  trend,
  lowerIsBetter,
  accent,
  loading,
}: StatProps): ReactElement {
  const good = trend === (lowerIsBetter ? 'down' : 'up');
  const bad = trend === (lowerIsBetter ? 'up' : 'down');
  const arrow = trend === 'up' ? '↑' : trend === 'down' ? '↓' : '';

  return (
    <div className={cn('surface-card overflow-hidden p-4', accent && 'border-border-strong')}>
      <div className="text-2xs text-muted-foreground font-medium uppercase tracking-[0.12em]">
        {label}
      </div>
      {loading ? (
        <div className="skeleton mt-2 h-7 w-24" />
      ) : (
        <div
          className={cn(
            'mt-1.5 font-mono text-2xl font-semibold tabular-nums leading-none',
            accent && 'text-primary',
          )}
        >
          {value}
        </div>
      )}
      {hint ? (
        <div
          className={cn(
            'mt-2 flex items-center gap-1 text-xs',
            good && 'text-ok',
            bad && 'text-danger',
            !good && !bad && 'text-muted-foreground',
          )}
        >
          {arrow ? <span aria-hidden="true">{arrow}</span> : null}
          {hint}
        </div>
      ) : null}
    </div>
  );
}

const BADGE_TONES = {
  ok: 'border-ok/30 bg-ok/10 text-ok',
  warn: 'border-warn/30 bg-warn/10 text-warn',
  danger: 'border-danger/30 bg-danger/10 text-danger',
  info: 'border-primary/30 bg-primary/10 text-primary',
  // `neutral` replaces what used to be a second brand colour. A badge that
  // is merely a label should not compete with one that reports status.
  neutral: 'border-border-strong bg-elevated text-foreground/85',
  muted: 'border-border-strong bg-muted/50 text-muted-foreground',
} as const;

export type BadgeTone = keyof typeof BADGE_TONES;

export function Badge({
  children,
  tone = 'muted',
  className,
  ...rest
}: {
  children: ReactNode;
  tone?: BadgeTone;
  className?: string;
} & Record<string, unknown>): ReactElement {
  // Extra props are forwarded so callers can attach `data-*` attributes —
  // without this a `data-testid` on a Badge is silently dropped and the element
  // becomes unaddressable from a test.
  return (
    <span
      {...rest}
      className={cn(
        'text-2xs inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-0.5 font-medium',
        BADGE_TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

const BUTTON_VARIANTS = {
  default: 'border-border-strong bg-elevated hover:bg-muted hover:border-muted-foreground/30',
  primary:
    'border-primary/40 bg-primary/15 text-primary hover:bg-primary/25 hover:border-primary/60',
  danger: 'border-danger/40 bg-danger/10 text-danger hover:bg-danger/20 hover:border-danger/60',
  ghost: 'border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground',
} as const;

export function Button({
  children,
  onClick,
  variant = 'default',
  disabled,
  className,
  type = 'button',
  size = 'md',
  ...rest
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: keyof typeof BUTTON_VARIANTS;
  disabled?: boolean;
  className?: string;
  type?: 'button' | 'submit';
  size?: 'sm' | 'md';
} & Record<string, unknown>): ReactElement {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-md border font-medium transition-all duration-150',
        'focus-visible:ring-primary/50 focus-visible:outline-none focus-visible:ring-2',
        'active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40',
        size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-1.5 text-sm',
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

// Flat fills. A gradient inside a 6px bar is invisible detail that only
// costs a paint.
const METER_FILLS: Record<BadgeTone, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  danger: 'bg-danger',
  info: 'bg-primary',
  neutral: 'bg-muted-foreground',
  muted: 'bg-muted-foreground/60',
};

/** A labelled progress bar, used for utilisation and quality scores. */
export function Meter({
  value,
  max = 1,
  tone = 'info',
  label,
  showValue = true,
}: {
  value: number;
  max?: number;
  tone?: BadgeTone;
  label?: string;
  showValue?: boolean;
}): ReactElement {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="flex items-center gap-2.5">
      <div
        className="bg-muted/80 h-1.5 flex-1 overflow-hidden rounded-full"
        role="meter"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label ?? 'value'}
      >
        <motion.div
          className={cn('h-full rounded-full', METER_FILLS[tone])}
          initial={{ width: 0 }}
          animate={{ width: `${String(pct)}%` }}
          transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        />
      </div>
      {showValue ? (
        <span className="text-2xs text-muted-foreground w-11 shrink-0 text-right font-mono tabular-nums">
          {pct.toFixed(0)}%
        </span>
      ) : null}
    </div>
  );
}

export function Table({
  headers,
  children,
}: {
  headers: string[];
  children: ReactNode;
}): ReactElement {
  return (
    // Wide tables scroll inside their own container so the page body never
    // scrolls horizontally.
    <div className="surface-card overflow-x-auto p-0">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-border bg-elevated/60 border-b">
            {headers.map((header, index) => (
              <th
                key={`${header}-${String(index)}`}
                className="text-2xs text-muted-foreground whitespace-nowrap px-3.5 py-2.5 text-left font-semibold uppercase tracking-[0.1em]"
              >
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-border/70 divide-y">{children}</tbody>
      </table>
    </div>
  );
}

export function Row({
  children,
  ...rest
}: { children: ReactNode } & Record<string, unknown>): ReactElement {
  return (
    <tr className="hover:bg-elevated/50 transition-colors" {...rest}>
      {children}
    </tr>
  );
}

export function EmptyState({
  message,
  action,
}: {
  message: string;
  action?: ReactNode;
}): ReactElement {
  return (
    <div className="surface-card flex flex-col items-center gap-3 px-6 py-14 text-center">
      <div className="border-border-strong bg-elevated text-muted-foreground grid size-10 place-items-center rounded-lg border">
        <svg
          viewBox="0 0 24 24"
          className="size-5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="M4 7h16M4 12h10M4 17h7" strokeLinecap="round" />
        </svg>
      </div>
      <p className="text-muted-foreground max-w-sm text-sm">{message}</p>
      {action}
    </div>
  );
}

/**
 * A shape that matches the content it replaces.
 *
 * Preferred over a spinner because the layout does not jump when data lands,
 * and the page reads as "arriving" rather than "broken".
 */
export function Skeleton({
  rows = 3,
  className,
}: {
  rows?: number;
  className?: string;
}): ReactElement {
  return (
    <div className={cn('space-y-3', className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="surface-card flex items-center gap-4 p-4">
          <div className="skeleton size-9 shrink-0 rounded-md" />
          <div className="flex-1 space-y-2">
            <div className="skeleton h-3 w-1/3" />
            <div className="skeleton h-2.5 w-1/2" />
          </div>
          <div className="skeleton h-6 w-16" />
        </div>
      ))}
    </div>
  );
}

export function StatSkeleton({ count = 4 }: { count?: number }): ReactElement {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="surface-card p-4">
          <div className="skeleton h-2.5 w-20" />
          <div className="skeleton mt-3 h-7 w-24" />
          <div className="skeleton mt-3 h-2.5 w-16" />
        </div>
      ))}
    </div>
  );
}

export function Spinner({ label = 'Loading' }: { label?: string }): ReactElement {
  return (
    <div className="text-muted-foreground flex items-center gap-2.5 text-sm" role="status">
      <span className="border-muted-foreground/30 border-t-primary size-3.5 animate-spin rounded-full border-2" />
      {label}…
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }): ReactElement {
  const message = error instanceof Error ? error.message : 'Something went wrong.';
  return (
    <div className="surface-card border-danger/30 bg-danger/5 flex items-start gap-3 p-4">
      <div className="bg-danger/15 text-2xs text-danger mt-0.5 grid size-5 shrink-0 place-items-center rounded-full font-bold">
        !
      </div>
      <div>
        <p className="text-danger text-sm font-medium">Request failed</p>
        <p className="text-muted-foreground mt-0.5 text-xs">{message}</p>
      </div>
    </div>
  );
}

/** Fades and lifts a page on mount. Kept small — motion should not be a feature. */
export function PageTransition({ children }: { children: ReactNode }): ReactElement {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  );
}

/** A right-hand drawer. Used for every detail view, so they all behave alike. */
export function Drawer({
  title,
  subtitle,
  onClose,
  children,
  width = 'max-w-2xl',
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  width?: string;
}): ReactElement {
  return (
    <div
      className="bg-background/75 fixed inset-0 z-50 flex justify-end backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <motion.div
        initial={{ x: 32, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        className={cn(
          'border-border-strong bg-surface h-full w-full overflow-y-auto border-l',
          width,
        )}
        onClick={(event) => {
          event.stopPropagation();
        }}
        role="dialog"
        aria-label={title}
      >
        <div className="border-border bg-surface/95 sticky top-0 z-10 flex items-start justify-between gap-4 border-b px-6 py-4 backdrop-blur">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold tracking-tight">{title}</h2>
            {subtitle ? (
              <p className="text-muted-foreground truncate font-mono text-xs">{subtitle}</p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-md p-1 transition-colors"
          >
            <svg
              viewBox="0 0 24 24"
              className="size-4"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        <div className="px-6 py-5">{children}</div>
      </motion.div>
    </div>
  );
}

/** A labelled key/value pair, for the dense detail grids inside drawers. */
export function Field({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <div className="min-w-0">
      <dt className="text-2xs text-muted-foreground uppercase tracking-[0.1em]">{label}</dt>
      <dd className="mt-0.5 truncate text-sm">{children}</dd>
    </div>
  );
}
