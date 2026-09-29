/**
 * Idle Cost Leaderboard: which vehicles are costing money, worst first
 * (CLAUDE.md S14).
 *
 * The one view that answers "where do I start?". Ranking by estimated cost rather than
 * by idle hours is deliberate: a heavy truck idling one hour costs more than a car
 * idling three, so hours alone would send an operator to the wrong vehicle.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
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
  formatHours,
  formatMoney,
} from '../components/common';

const HEADERS = [
  '#',
  'Vehicle',
  'Class',
  'Depot',
  'Idle time',
  'Fuel',
  'Estimated cost',
  'Excessive',
] as const;

export function IdleLeaderboard({ days }: { days: number }): ReactNode {
  const [limit, setLimit] = useState(20);
  const [selectedVin, setSelectedVin] = useState<string | null>(null);

  const leaderboard = useQuery({
    queryKey: ['leaderboard', days, limit],
    queryFn: () => api.leaderboard(days, limit),
  });

  if (leaderboard.isPending) return <LoadingState label="Ranking vehicles by idle cost…" />;
  if (leaderboard.isError) {
    return (
      <ErrorState error={leaderboard.error} onRetry={() => void leaderboard.refetch()} />
    );
  }

  const { items, excludes_bev: excludesBev } = leaderboard.data;

  if (items.length === 0) {
    return (
      <EmptyState
        title="No priced idle events in this window"
        hint="Either no vehicle idled beyond the threshold, or no fuel price is configured so no cost could be estimated."
      />
    );
  }

  return (
    <>
      <div className="toolbar">
        <label className="field">
          <span className="field__label">Show</span>
          <select
            className="select"
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
          >
            <option value={10}>Top 10</option>
            <option value={20}>Top 20</option>
            <option value={50}>Top 50</option>
          </select>
        </label>
        {excludesBev && (
          <p className="notice notice--inline">
            Electric vehicles are absent: they have no fuel cost to rank by.
          </p>
        )}
      </div>

      <DataTable caption={`Vehicles by estimated idle cost, last ${days} days`} headers={HEADERS}>
        {items.map((row) => (
          <tr
            key={row.vin}
            className={selectedVin === row.vin ? 'is-selected' : undefined}
            onClick={() => setSelectedVin(row.vin === selectedVin ? null : row.vin)}
          >
            <td className="num">{row.rank}</td>
            <td>
              <code className="vin">{row.vin}</code>
              <span className="subtle"> {row.registration}</span>
            </td>
            <td>{row.vehicle_class.replace(/_/g, ' ')}</td>
            <td>{row.fleet_name}</td>
            <td className="num">{formatHours(row.total_idle_hours)}</td>
            <td className="num">{row.total_fuel_litres.toFixed(1)} L</td>
            <td className="num strong">{formatMoney(row.total_cost, row.currency)}</td>
            <td className="num">
              {row.excessive_count > 0 ? (
                <Badge tone="warn">{row.excessive_count}</Badge>
              ) : (
                <span className="subtle">—</span>
              )}
            </td>
          </tr>
        ))}
      </DataTable>

      {selectedVin && <VehicleIdleDetail vin={selectedVin} days={days} />}
    </>
  );
}

/**
 * The individual events behind one vehicle's total.
 *
 * This is the traceability step that makes the leaderboard trustworthy: a user can go
 * from a fleet figure to the specific idle periods that produced it, each with its own
 * assumptions and data-quality signal.
 */
function VehicleIdleDetail({ vin, days }: { vin: string; days: number }): ReactNode {
  const events = useQuery({
    queryKey: ['idle-events', vin, days],
    queryFn: () => api.idleEvents({ days, vin, pageSize: 20 }),
  });

  return (
    <section className="panel" aria-label={`Idle events for ${vin}`}>
      <h2 className="panel__title">
        Idle events for <code className="vin">{vin}</code>
      </h2>

      {events.isPending && <LoadingState label="Loading idle events…" />}
      {events.isError && (
        <ErrorState error={events.error} onRetry={() => void events.refetch()} />
      )}

      {events.isSuccess &&
        (events.data.items.length === 0 ? (
          <EmptyState
            title="No idle events for this vehicle in the window"
            hint="Its cost total may come from an earlier period."
          />
        ) : (
          <>
            <DataTable
              caption={`Individual idle events for ${vin}`}
              headers={['Started', 'Duration', 'Fuel (est)', 'Fuel (device)', 'Cost', 'Notes']}
            >
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
                    {event.fuel_litres_est !== null
                      ? `${event.fuel_litres_est.toFixed(3)} L`
                      : '—'}
                  </td>
                  <td className="num">
                    {/* The D2 cross-check. Shown beside the estimate so a wrong
                        configured burn rate is visible rather than silent. */}
                    {event.fuel_litres_measured !== null
                      ? `${event.fuel_litres_measured.toFixed(3)} L`
                      : <span className="subtle">not reported</span>}
                  </td>
                  <td className="num strong">
                    {event.bev_excluded ? (
                      <Badge tone="info">electric</Badge>
                    ) : (
                      formatMoney(event.cost_est, event.assumptions.currency)
                    )}
                  </td>
                  <td className="subtle small">
                    {event.max_gap_seconds > 30 && (
                      <>gap {Math.round(event.max_gap_seconds)}s · </>
                    )}
                    {event.sample_count} samples
                    {event.location_coarsened && ' · location coarsened'}
                  </td>
                </tr>
              ))}
            </DataTable>
            {events.data.items[0] && (
              <AssumptionsNote assumptions={events.data.items[0].assumptions} />
            )}
          </>
        ))}
    </section>
  );
}
