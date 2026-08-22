/**
 * Display formatting.
 *
 * Separate from the component module so that file exports components only,
 * which is what React Fast Refresh needs to hot-reload it. Cost uses more
 * decimal places than currency formatting gives, because a single completion
 * costs fractions of a cent and rounding to two places shows $0.00.
 */

export const fmt = {
  int: (value: number): string => Math.round(value).toLocaleString(),
  float: (value: number, digits = 1): string => value.toFixed(digits),
  pct: (value: number, digits = 1): string => `${(value * 100).toFixed(digits)}%`,
  ms: (value: number): string => `${Math.round(value).toLocaleString()} ms`,
  /** Cost needs more precision than currency formatting gives at these scales. */
  usd: (value: number, digits = 4): string => `$${value.toFixed(digits)}`,
  perMtok: (value: number): string => `$${value.toFixed(2)}/M`,
  compact: (value: number): string =>
    new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value),
  time: (iso: string): string =>
    new Date(iso).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }),
  date: (iso: string): string => new Date(iso).toLocaleDateString('en', { dateStyle: 'medium' }),
};
