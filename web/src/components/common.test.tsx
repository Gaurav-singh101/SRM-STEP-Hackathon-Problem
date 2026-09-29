/**
 * Tests for the presentation rules that decide whether a number is honest.
 *
 * Scoped deliberately. Rendering every page in jsdom would mostly re-test React, whereas
 * these are the places where a bug produces a *wrong or misleading figure* on a finance
 * user's screen: money losing precision, an assumption not being shown beside the cost it
 * explains, and a partial total presenting as complete.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Assumptions } from '../api/types';
import {
  AssumptionsNote,
  Badge,
  EmptyState,
  KpiCard,
  LoadingState,
  PartialTotalNotice,
  formatHours,
  formatMoney,
} from './common';

const ASSUMPTIONS: Assumptions = {
  formula_version: 'idle-fuel-v1',
  burn_rate_lph: 1.5,
  fuel_price_per_litre: '92.50',
  price_id: 'inr-diesel-current',
  idle_threshold_seconds: 180,
  currency: 'INR',
};

describe('formatMoney', () => {
  it('never parses the amount to a float', () => {
    // 0.1 + 0.2 territory. A value beyond double precision must survive intact as far as
    // the two places actually displayed, which parsing would not guarantee (D11).
    expect(formatMoney('9007199254740993.4567', 'INR')).toBe('9,007,199,254,740,993.45 INR');
  });

  it('groups thousands', () => {
    expect(formatMoney('3733.5017', 'INR')).toBe('3,733.50 INR');
    expect(formatMoney('999.0000', 'INR')).toBe('999.00 INR');
    expect(formatMoney('1000000.0000', 'INR')).toBe('1,000,000.00 INR');
  });

  it('truncates rather than rounds, so a displayed figure never exceeds the real one', () => {
    // 0.999 shows as 0.99. Rounding up would print a cost higher than the API reported,
    // and a finance figure that overstates itself is the worse of the two errors.
    expect(formatMoney('0.9999', null)).toBe('0.99');
  });

  it('pads a single decimal place so amounts line up in a column', () => {
    expect(formatMoney('12.5', 'INR')).toBe('12.50 INR');
  });

  it('shows an em dash for an absent amount, not zero', () => {
    // A BEV has no fuel cost. Rendering that as "0.00" would read as a measured zero.
    expect(formatMoney(null, 'INR')).toBe('—');
  });

  it('omits the currency when none is known', () => {
    expect(formatMoney('10.0000', null)).toBe('10.00');
  });
});

describe('formatHours', () => {
  it('switches to minutes below an hour, where a decimal hour is unreadable', () => {
    expect(formatHours(0.25)).toBe('15 min');
    expect(formatHours(0.999)).toBe('60 min');
  });

  it('shows one decimal place at an hour and above', () => {
    expect(formatHours(1)).toBe('1.0 h');
    expect(formatHours(23.09)).toBe('23.1 h');
  });
});

describe('AssumptionsNote', () => {
  it('states the threshold, burn rate and price so a figure can be reproduced', () => {
    render(<AssumptionsNote assumptions={ASSUMPTIONS} />);
    expect(screen.getByText('idle-fuel-v1')).toBeInTheDocument();
    expect(screen.getByText(/180s \(3 min\)/)).toBeInTheDocument();
    expect(screen.getByText(/1\.5 litres\/hour/)).toBeInTheDocument();
    expect(screen.getByText('inr-diesel-current')).toBeInTheDocument();
  });

  it('calls the figure an estimate in words, not only by omission', () => {
    render(<AssumptionsNote assumptions={ASSUMPTIONS} />);
    expect(screen.getByText(/Estimated/)).toBeInTheDocument();
    expect(screen.getByText(/not a measurement/)).toBeInTheDocument();
  });

  it('omits the burn rate row when there is no burn rate rather than showing a blank', () => {
    render(
      <AssumptionsNote assumptions={{ ...ASSUMPTIONS, burn_rate_lph: null, price_id: null }} />,
    );
    expect(screen.queryByText(/litres\/hour/)).not.toBeInTheDocument();
  });
});

describe('PartialTotalNotice', () => {
  it('says how many vehicles a total leaves out', () => {
    render(<PartialTotalNotice excludedCount={14} />);
    expect(screen.getByText(/Excludes 14 electric vehicles/)).toBeInTheDocument();
  });

  it('uses the singular for one vehicle', () => {
    render(<PartialTotalNotice excludedCount={1} />);
    expect(screen.getByText(/Excludes 1 electric vehicle:/)).toBeInTheDocument();
  });

  it('renders nothing when the total is complete', () => {
    // Silence is correct here: a notice saying "excludes 0 vehicles" is noise that trains
    // a reader to skip the notice when it matters.
    const { container } = render(<PartialTotalNotice excludedCount={0} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('states', () => {
  it('gives a loading state an accessible status role', () => {
    render(<LoadingState label="Loading idle events…" />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading idle events…');
  });

  it('gives an empty state a reason, not just a title', () => {
    // CLAUDE.md S14: an empty view that does not say why is indistinguishable from a bug.
    render(<EmptyState title="No idle events" hint="Nothing idled beyond the threshold." />);
    expect(screen.getByText('No idle events')).toBeInTheDocument();
    expect(screen.getByText('Nothing idled beyond the threshold.')).toBeInTheDocument();
  });
});

describe('Badge and KpiCard', () => {
  it('carries its meaning as text, never as colour alone', () => {
    render(<Badge tone="warn">excessive</Badge>);
    expect(screen.getByText('excessive')).toBeInTheDocument();
  });

  it('shows the caveat that makes a KPI honest', () => {
    render(<KpiCard label="Estimated idle cost" value="3,733.50 INR" hint="An estimate" />);
    expect(screen.getByText('An estimate')).toBeInTheDocument();
  });
});
