import type { Config } from 'tailwindcss';

/**
 * Design tokens live in `src/index.css` as CSS variables and are surfaced here
 * as Tailwind scales, so a token change propagates everywhere without a
 * find-and-replace — and so an alpha modifier (`bg-primary/10`) works on every
 * one of them.
 */
export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        background: 'hsl(var(--background))',
        surface: 'hsl(var(--surface))',
        elevated: 'hsl(var(--elevated))',
        foreground: 'hsl(var(--foreground))',
        muted: { DEFAULT: 'hsl(var(--muted))', foreground: 'hsl(var(--muted-foreground))' },
        card: { DEFAULT: 'hsl(var(--card))', foreground: 'hsl(var(--card-foreground))' },
        border: { DEFAULT: 'hsl(var(--border))', strong: 'hsl(var(--border-strong))' },
        primary: { DEFAULT: 'hsl(var(--primary))', foreground: 'hsl(var(--primary-foreground))' },
        ok: 'hsl(var(--ok))',
        warn: 'hsl(var(--warn))',
        danger: 'hsl(var(--danger))',
        chart: {
          1: 'hsl(var(--chart-1))',
          2: 'hsl(var(--chart-2))',
          3: 'hsl(var(--chart-3))',
          4: 'hsl(var(--chart-4))',
          5: 'hsl(var(--chart-5))',
        },
      },
      fontFamily: {
        sans: ['Inter var', 'Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        // A tighter scale than Tailwind's default. Dense operational UI needs
        // small text to stay legible, so the small end is nudged up and the
        // large end pulled in.
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.01em' }],
        xs: ['0.75rem', { lineHeight: '1.1rem' }],
        sm: ['0.8125rem', { lineHeight: '1.25rem' }],
        base: ['0.9375rem', { lineHeight: '1.5rem' }],
        lg: ['1.0625rem', { lineHeight: '1.6rem', letterSpacing: '-0.01em' }],
        xl: ['1.375rem', { lineHeight: '1.8rem', letterSpacing: '-0.02em' }],
        '2xl': ['1.75rem', { lineHeight: '2.1rem', letterSpacing: '-0.025em' }],
        '3xl': ['2.25rem', { lineHeight: '2.5rem', letterSpacing: '-0.03em' }],
        '4xl': ['3rem', { lineHeight: '1.08', letterSpacing: '-0.035em' }],
        '5xl': ['3.75rem', { lineHeight: '1.05', letterSpacing: '-0.04em' }],
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 3px)',
        sm: 'calc(var(--radius) - 5px)',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: { 'fade-up': 'fade-up 0.3s cubic-bezier(0.16, 1, 0.3, 1)' },
    },
  },
  plugins: [],
} satisfies Config;
