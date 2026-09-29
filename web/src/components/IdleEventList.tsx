/**
 * The individual idle events behind one vehicle's total.
 *
 * Shared rather than copied: it appears as a drill-down under the leaderboard and as the
 * main table on the Vehicle Detail page, and CLAUDE.md S7 forbids a per-page copy of the
 * same fetch and the same columns. It also means the D2 cross-check column - estimated
 * fuel beside device-reported fuel - can never be present in one place and missing in the
 * other.
 *
 * This is the traceability step that makes a fleet figure trustworthy: a user can go from
 * one cost total to the specific idle periods that produced it, each with its own
 * assumptions and data-quality signal.
 */

import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { api } from '../api/client';
import {
  AssumptionsNote,
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  formatDateTime,
  formatMoney,
} from './common';

const HEADERS = [
  'Started',
  'Duration',
  'Fuel (est)',
  'Fuel (device)',
  'Cost',
  'Notes',
] as const;

export function IdleEventList({
  vin,
  days,
  pageSize = 20,
  emptyHint,
}: {
  vin: string;
  days: number;
  pageSize?: number;
  emptyHint?: string;
}): ReactNode {
  const events = useQuery({
    queryKey: ['idle-events', vin, days, pageSize],
    queryFn: () => api.idleEvents({ days, vin, pageSize }),
  });

  if (events.isPending) return <LoadingState label="Loading idle events…" />;
  if (events.isError) {
    return <ErrorState error={events.error} onRetry={() => void events.refetch()} />;
  }

  if (events.data.items.length === 0) {
    return (
      <EmptyState
        title="No idle events for this vehicle in the window"
        hint={emptyHint ?? 'Its cost total may come from an earlier period.'}
      />
    );
  }

  const first = events.data.items[0];

  return (
    <>
      <DataTable caption={`Individual idle events for ${vin}`} headers={HEADERS}>
        {events.data.items.map((event) => (
          <tr key={event.idle_event_id}>
            <td>{formatDateTime(event.started_at)}</td>
            <td className="num">
              {event.duration_minutes.toFixed(1)} min
              {event.excessive && (
                <>
                  {' '}
                  <Badge tone="warn">excessive</Badge>
                </>
              )}
            </td>
            <td className="num">
              {event.fuel_litres_est !== null ? `${event.fuel_litres_est.toFixed(3)} L` : '—'}
            </td>
            <td className="num">
              {/* The D2 cross-check. Shown beside the estimate so a wrong configured
                  burn rate is visible rather than silent. */}
              {event.fuel_litres_measured !== null ? (
                `${event.fuel_litres_measured.toFixed(3)} L`
              ) : (
                <span className="subtle">not reported</span>
              )}
            </td>
            <td className="num strong">
              {event.bev_excluded ? (
                <Badge tone="info">electric</Badge>
              ) : (
                formatMoney(event.cost_est, event.assumptions.currency)
              )}
            </td>
            <td className="subtle small">
              {event.max_gap_seconds > 30 && <>gap {Math.round(event.max_gap_seconds)}s · </>}
              {event.sample_count} samples
              {event.location_coarsened && ' · location coarsened'}
            </td>
          </tr>
        ))}
      </DataTable>
      {first && <AssumptionsNote assumptions={first.assumptions} />}
    </>
  );
}
