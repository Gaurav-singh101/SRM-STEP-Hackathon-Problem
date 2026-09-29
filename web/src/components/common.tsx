/**
 * Shared presentation components.
 *
 * CLAUDE.md S7 forbids per-page copies of these, and S14 requires loading, empty and
 * error states on every view - which only stays true if there is one implementation of
 * each to reuse.
 */

import type { ReactNode } from 'react';

import { ApiError } from '../api/client';
import type { Assumptions } from '../api/types';

/**
 * Format an exact decimal amount for display.
 *
 * Takes a string and inserts separators without parsing to a float, because parsing is
 * exactly what loses precision. Display only - never arithmetic (D11).
 */
export function formatMoney(amount: string | null, currency: string | null): string {
  if (amount === null) return '—';
  // Defaults on both parts: `noUncheckedIndexedAccess` types array elements as possibly
  // undefined, and it is right to - a caller could pass an empty string.
  const [whole = '0', fraction = ''] = amount.split('.');
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  // Two decimal places for display; the full precision stays in the API response.
  const shown = fraction ? `${grouped}.${fraction.slice(0, 2).padEnd(2, '0')}` : grouped;
  return currency ? `${shown} ${currency}` : shown;
}

export function formatHours(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  return `${hours.toFixed(1)} h`;
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

/** A skeleton, not a spinner: it holds the layout so content does not jump in. */
export function LoadingState({ label }: { label: string }): ReactNode {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/**
 * Empty state.
 *
 * Says why it is empty and what to do, because "No data" leaves a user unable to tell a
 * working-but-idle system from a broken one.
 */
export function EmptyState({ title, hint }: { title: string; hint: string }): ReactNode {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      <p className="state__hint">{hint}</p>
    </div>
  );
}

/**
 * Error state.
 *
 * Shows the correlation id when the API supplied one: it turns "it broke" into a
 * reportable, findable request.
 */
export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}): ReactNode {
  const apiError = error instanceof ApiError ? error : null;
  const message = apiError?.message ?? 'Something went wrong';

  return (
    <div className="state state--error" role="alert">
      <p className="state__title">{message}</p>
      {apiError?.status === 401 && (
        <p className="state__hint">Your session is not valid. Sign in again to continue.</p>
      )}
      {apiError?.correlationId && (
        <p className="state__hint">
          Reference: <code>{apiError.correlationId}</code>
        </p>
      )}
      {onRetry && apiError?.isRetryable !== false && (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

/** A single KPI. `hint` carries the caveat that makes the number honest. */
export function KpiCard({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'neutral' | 'warn' | 'good';
}): ReactNode {
  return (
    <div className={`kpi kpi--${tone}`}>
      <p className="kpi__label">{label}</p>
      <p className="kpi__value">{value}</p>
      {hint && <p className="kpi__hint">{hint}</p>}
    </div>
  );
}

/**
 * The assumptions behind a monetary figure.
 *
 * Rendered wherever cost appears, not tucked into a help page. CLAUDE.md S14 requires
 * it and the reason is practical: a finance manager who cannot reproduce a number will
 * not act on it.
 */
export function AssumptionsNote({
  assumptions,
  className,
}: {
  assumptions: Assumptions;
  className?: string;
}): ReactNode {
  const thresholdMinutes = Math.round(assumptions.idle_threshold_seconds / 60);
  return (
    <details className={`assumptions ${className ?? ''}`}>
      <summary>
        Estimated &middot; formula <code>{assumptions.formula_version}</code>
      </summary>
      <dl className="assumptions__list">
        <dt>Idle threshold</dt>
        <dd>
          {assumptions.idle_threshold_seconds}s ({thresholdMinutes} min) of continuous
          engine-on, stationary time
        </dd>

        {assumptions.burn_rate_lph !== null && (
          <>
            <dt>Idle burn rate</dt>
            <dd>{assumptions.burn_rate_lph} litres/hour for this vehicle class</dd>
          </>
        )}

        {assumptions.price_id && (
          <>
            <dt>Fuel price</dt>
            <dd>
              <code>{assumptions.price_id}</code>
            </dd>
          </>
        )}

        <dt>Basis</dt>
        <dd>
          An <strong>estimate</strong>, not a measurement. Burn rates are proposed
          synthetic values, and durations count only observed samples, so a figure
          under-states rather than over-states.
        </dd>
      </dl>
    </details>
  );
}

/**
 * The BEV exclusion notice.
 *
 * Shown beside any fleet total that omits electric vehicles. A total presented as
 * complete when it is not is simply a wrong number, and a finance user would under-read
 * fleet cost without ever knowing (D9).
 */
export function PartialTotalNotice({ excludedCount }: { excludedCount: number }): ReactNode {
  if (excludedCount <= 0) return null;
  return (
    <p className="notice notice--info">
      Excludes {excludedCount} electric {excludedCount === 1 ? 'vehicle' : 'vehicles'}:
      they consume no fuel at rest, so no idle fuel cost applies. They are still counted
      for utilisation.
    </p>
  );
}

/** A table that keeps its header, caption and scope attributes for screen readers. */
export function DataTable({
  caption,
  headers,
  children,
}: {
  caption: string;
  headers: readonly string[];
  children: ReactNode;
}): ReactNode {
  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="table__caption">{caption}</caption>
        <thead>
          <tr>
            {headers.map((header) => (
              <th key={header} scope="col">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'warn' | 'good' | 'info';
}): ReactNode {
  return <span className={`badge badge--${tone}`}>{children}</span>;
}
