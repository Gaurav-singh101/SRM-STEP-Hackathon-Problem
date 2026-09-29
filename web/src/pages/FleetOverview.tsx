/**
 * Fleet Overview: the KPIs and trend a finance manager opens first (CLAUDE.md S14).
 *
 * Every monetary figure carries its assumptions and is labelled an estimate. Where the
 * cost total omits electric vehicles, the page says so next to the number rather than in
 * a footnote.
 */

import { useQuery } from '@tanstack/react-query';
import { Suspense, lazy } from 'react';
import type { ReactNode } from 'react';

import { api } from '../api/client';
import {
  AssumptionsNote,
  EmptyState,
  ErrorState,
  KpiCard,
  LoadingState,
  PartialTotalNotice,
  formatHours,
  formatMoney,
} from '../components/common';

/**
 * Lazy: recharts is ~530 kB minified, larger than the rest of the app combined, and only
 * this view needs it. A user who goes straight to the leaderboard never downloads it.
 */
const IdleTrendChart = lazy(() => import('../components/IdleTrendChart'));

export function FleetOverview({ days }: { days: number }): ReactNode {
  const summary = useQuery({
    queryKey: ['fleet-summary', days],
    queryFn: () => api.fleetSummary(days),
  });

  const trend = useQuery({
    queryKey: ['fleet-trend', days],
    queryFn: () => api.fleetTrend(days),
  });

  if (summary.isPending) return <LoadingState label="Loading fleet summary…" />;
  if (summary.isError) {
    return <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />;
  }

  const data = summary.data;

  // Distinguishes "no idling in this window" from "no data at all". The first is good
  // news; the second usually means the pipeline has not run, and a single "No data"
  // message for both would leave a user unable to tell which.
  if (data.idle_event_count === 0) {
    return (
      <>
        <section className="kpi-grid">
          <KpiCard label="Active vehicles" value={data.vehicle_count.toLocaleString()} />
        </section>
        <EmptyState
          title={`No idle events in the last ${days} days`}
          hint={
            data.vehicle_count === 0
              ? 'No vehicles are registered for this tenant. Run the seed job to load the reference fleet.'
              : 'Either the fleet genuinely did not idle beyond the threshold, or the processor has not consumed any telemetry yet.'
          }
        />
      </>
    );
  }

  const idlePerVehicle =
    data.vehicles_with_idle > 0 ? data.total_idle_hours / data.vehicles_with_idle : 0;

  return (
    <>
      <section className="kpi-grid" aria-label="Fleet key figures">
        <KpiCard
          label="Estimated idle cost"
          value={formatMoney(data.total_cost, data.currency)}
          hint={`over ${days} days · ${data.idle_event_count.toLocaleString()} idle events`}
          tone="warn"
        />
        <KpiCard
          label="Fuel lost to idling"
          value={`${data.total_fuel_litres.toFixed(1)} L`}
          hint={`${formatHours(data.total_idle_hours)} of billable idle`}
        />
        <KpiCard
          label="Vehicles idling"
          value={`${data.vehicles_with_idle.toLocaleString()} / ${data.vehicle_count.toLocaleString()}`}
          hint={`${formatHours(idlePerVehicle)} each on average`}
        />
        <KpiCard
          label="Excessive idle events"
          value={data.excessive_count.toLocaleString()}
          hint={`${data.open_alert_count} alert${data.open_alert_count === 1 ? '' : 's'} awaiting action`}
          tone={data.excessive_count > 0 ? 'warn' : 'good'}
        />
      </section>

      <PartialTotalNotice excludedCount={data.excluded_bev_count} />
      <AssumptionsNote assumptions={data.assumptions} />

      <section className="panel" aria-label="Daily idle cost trend">
        <h2 className="panel__title">Daily idle cost and hours</h2>
        {trend.isPending && <LoadingState label="Loading trend…" />}
        {trend.isError && (
          <ErrorState error={trend.error} onRetry={() => void trend.refetch()} />
        )}
        {trend.isSuccess &&
          (trend.data.items.length === 0 ? (
            <EmptyState
              title="No daily data in this window"
              hint="Idle events exist but fall outside the charted range."
            />
          ) : (
            <Suspense fallback={<LoadingState label="Loading chart…" />}>
              <IdleTrendChart trend={trend.data} />
            </Suspense>
          ))}
      </section>
    </>
  );
}
