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
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  formatHours,
  formatMoney,
} from '../components/common';
import { IdleEventList } from '../components/IdleEventList';

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

export function IdleLeaderboard({
  days,
  onOpenVehicle,
}: {
  days: number;
  onOpenVehicle: (vin: string) => void;
}): ReactNode {
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
              {/* A button, not a clickable cell: keyboard users need a real control,
                  and the row's own click only expands the drill-down. */}
              <button
                type="button"
                className="link"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpenVehicle(row.vin);
                }}
              >
                <code className="vin">{row.vin}</code>
              </button>
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

      {selectedVin && (
        <section className="panel" aria-label={`Idle events for ${selectedVin}`}>
          <h2 className="panel__title">
            Idle events for <code className="vin">{selectedVin}</code>
          </h2>
          <IdleEventList vin={selectedVin} days={days} />
        </section>
      )}
    </>
  );
}
