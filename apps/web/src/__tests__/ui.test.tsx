import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Badge, Meter, Stat } from '@/components/ui';
import { fmt } from '@/lib/format';

describe('Stat', () => {
  it('colours a trend by whether lower is better', () => {
    // Rising cost and rising throughput are not both good. Getting this
    // backwards is the fastest way to make a dashboard actively misleading.
    const { rerender } = render(
      <Stat label="Cost" value="$0.19/M" hint="down 8%" trend="down" lowerIsBetter />,
    );
    expect(screen.getByText('down 8%')).toHaveClass('text-ok');

    rerender(<Stat label="Cost" value="$0.31/M" hint="up 12%" trend="up" lowerIsBetter />);
    expect(screen.getByText('up 12%')).toHaveClass('text-danger');
  });

  it('treats a rising value as good when higher is better', () => {
    render(<Stat label="Throughput" value="3.1k" hint="up 4%" trend="up" />);
    expect(screen.getByText('up 4%')).toHaveClass('text-ok');
  });
});

describe('Meter', () => {
  it('clamps out-of-range values instead of overflowing', () => {
    render(<Meter value={1.4} label="utilisation" />);
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '100');
  });

  it('reports a percentage of its max', () => {
    render(<Meter value={0.05} max={0.2} label="share" />);
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '25');
  });
});

describe('Badge', () => {
  it('renders its tone', () => {
    render(<Badge tone="danger">breached</Badge>);
    expect(screen.getByText('breached')).toHaveClass('text-danger');
  });
});

describe('fmt', () => {
  it('gives cost enough precision to be non-zero', () => {
    // A single completion costs fractions of a cent; two decimal places would
    // render every Playground call as $0.00.
    expect(fmt.usd(0.000091, 6)).toBe('$0.000091');
  });

  it('formats percentages, milliseconds and compact counts', () => {
    expect(fmt.pct(0.974, 1)).toBe('97.4%');
    expect(fmt.ms(210.4)).toBe('210 ms');
    expect(fmt.compact(3_100_000)).toBe('3.1M');
    expect(fmt.perMtok(0.19)).toBe('$0.19/M');
  });
});
