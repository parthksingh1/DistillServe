/**
 * One chart theme, shared by every chart in the console.
 *
 * Recharts cannot read CSS custom properties, so the token values have to be
 * repeated here as literals. Repeating them *once* is the point: three pages
 * each carrying their own axis colour is exactly how a set of charts ends up
 * looking like three unrelated products.
 *
 * Neutral-first, matching the rest of the console. The accent leads a series,
 * grey supports it, and the semantic colours appear only where a value really
 * carries state.
 */

export const CHART = {
  accent: 'hsl(212 72% 60%)',
  neutral: 'hsl(218 11% 60%)',
  faint: 'hsl(218 12% 40%)',
  ok: 'hsl(152 45% 48%)',
  warn: 'hsl(38 78% 56%)',
  danger: 'hsl(358 62% 58%)',
  axis: 'hsl(218 11% 52%)',
  grid: 'hsl(220 13% 18%)',
} as const;

/** Recharts tooltip styling. Matches the `elevated` surface token. */
export const CHART_TOOLTIP = {
  background: 'hsl(220 14% 15%)',
  border: '1px solid hsl(220 12% 26%)',
  borderRadius: 8,
  fontSize: 12,
  boxShadow: '0 4px 16px -6px hsl(220 18% 3% / 0.6)',
} as const;

/** Shared axis props, so tick size and stroke never drift between charts. */
export const AXIS_PROPS = {
  tick: { fontSize: 10 },
  stroke: CHART.axis,
  tickLine: false,
} as const;
