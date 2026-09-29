/**
 * Utilisation: which vehicles are under-used, and which are reporting something odd
 * (CLAUDE.md S14).
 *
 * Ordered worst-first, because the point of the view is the bottom of the list. The
 * three components sit beside every score: a fleet manager deciding whether to sell a
 * van needs to see *why* it scored 24, not just that it did.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type { ReactNode } from 'react';

import { api } from '../api/client';
import type { UtilisationEntry } from '../api/types';
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
} from '../components/common';

const HEADERS = [
  'Vehicle',
  'Class',
  'Score',
  'Band',
  'Active days',
  'Moving',
  'Distance',
  'Idle share',
] as const;

type BandFilter = 'all' | 'low' | 'abnormal';

/** Colour is never the only signal: every band carries its name as text. */
function bandTone(band: string): 'warn' | 'good' | 'info' | 'neutral' {
  if (band === 'low') return 'warn';
  if (band === 'abnormal') return 'info';
  if (band === 'high') return 'good';
  return 'neutral';
}

/** A component bar, so three numbers read as a shape rather than as digits. */
function ComponentBar({ value, label }: { value: number; label: string }): ReactNode {
  const percent = Math.round(value * 100);
  return (
    <div className="bar" title={`${label}: ${percent}% of the class norm`}>
      <div className="bar__fill" style={{ width: `${percent}%` }} />
      <span className="bar__text">{percent}%</span>
    </div>
  );
}

export function Utilisation(): ReactNode {
  const [band, setBand] = useState<BandFilter>('all');

  const utilisation = useQuery({
    queryKey: ['utilisation', band],
    queryFn: () =>
      api.utilisation({ pageSize: 50, band: band === 'all' ? undefined : band }),
  });

  if (utilisation.isPending) return <LoadingState label="Loading utilisation scores…" />;
  if (utilisation.isError) {
    return (
      <ErrorState error={utilisation.error} onRetry={() => void utilisation.refetch()} />
    );
  }

  const { items } = utilisation.data;

  if (items.length === 0) {
    return (
      <>
        <BandFilterControl value={band} onChange={setBand} />
        <EmptyState
          title={
            band === 'all'
              ? 'No utilisation scores yet'
              : `No vehicles in the ${band} band`
          }
          hint={
            band === 'all'
              ? 'Scores are produced by the batch job. Run `python -m batch rollup` then `python -m batch utilisation`.'
              : 'That is good news rather than missing data.'
          }
        />
      </>
    );
  }

  const first = items[0];

  return (
    <>
      <BandFilterControl value={band} onChange={setBand} />

      <p className="notice notice--info">
        Vehicles with no telemetry in the window are <strong>not scored</strong>. A
        failed telematics unit sends nothing, and reporting that as an idle asset would
        send someone to sell a vehicle that is working perfectly well.
      </p>

      <DataTable
        caption={`Utilisation over the last ${first?.window_days ?? 30} days, lowest first`}
        headers={HEADERS}
      >
        {items.map((row) => (
          <UtilisationRow key={row.vin} row={row} />
        ))}
      </DataTable>

      {first && (
        <details className="assumptions">
          <summary>
            How this is scored &middot; <code>{first.formula_version}</code>
          </summary>
          <dl className="assumptions__list">
            <dt>Score</dt>
            <dd>
              40% active-day ratio, 35% moving hours, 25% distance &mdash; each measured
              against the median for the same vehicle class, so a heavy truck is never
              judged by a city car&apos;s norms.
            </dd>
            <dt>Low</dt>
            <dd>Below 40. Consistently doing less than its peers.</dd>
            <dt>Abnormal</dt>
            <dd>
              The vehicle&apos;s own signals disagree &mdash; out every day but barely
              moving, for instance. That is a data-quality or misuse signal rather than a
              level of use, so it is reported separately and never counted as low.
            </dd>
            <dt>Baseline</dt>
            <dd>
              Built from {first.baseline_sample_size} peers in the{' '}
              {first.vehicle_class.replace(/_/g, ' ')} class. A score from few peers is
              weaker than one from many.
            </dd>
          </dl>
        </details>
      )}
    </>
  );
}

function UtilisationRow({ row }: { row: UtilisationEntry }): ReactNode {
  return (
    <>
      <tr>
        <td>
          <code className="vin">{row.vin}</code>
          <span className="subtle"> {row.registration}</span>
        </td>
        <td>{row.vehicle_class.replace(/_/g, ' ')}</td>
        <td className="num strong">{row.score.toFixed(1)}</td>
        <td>
          <Badge tone={bandTone(row.band)}>{row.band}</Badge>
        </td>
        <td>
          <ComponentBar value={row.active_day_component} label="Active days" />
        </td>
        <td>
          <ComponentBar value={row.moving_hours_component} label="Moving hours" />
        </td>
        <td>
          <ComponentBar value={row.distance_component} label="Distance" />
        </td>
        <td className="num">{Math.round(row.idle_share * 100)}%</td>
      </tr>
      {row.abnormal_reason && (
        <tr className="row--note">
          <td colSpan={HEADERS.length}>
            <span className="subtle small">⚠ {row.abnormal_reason}</span>
          </td>
        </tr>
      )}
    </>
  );
}

function BandFilterControl({
  value,
  onChange,
}: {
  value: BandFilter;
  onChange: (next: BandFilter) => void;
}): ReactNode {
  return (
    <div className="toolbar">
      <label className="field">
        <span className="field__label">Show</span>
        <select
          className="select"
          value={value}
          onChange={(event) => onChange(event.target.value as BandFilter)}
        >
          <option value="all">All vehicles</option>
          <option value="low">Low utilisation only</option>
          <option value="abnormal">Abnormal only</option>
        </select>
      </label>
    </div>
  );
}
