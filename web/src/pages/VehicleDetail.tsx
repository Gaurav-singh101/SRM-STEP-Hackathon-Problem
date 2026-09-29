/**
 * Vehicle Detail: one vehicle, its cost and its utilisation side by side
 * (CLAUDE.md S14, the fifth view).
 *
 * Both halves together, deliberately. Cost alone cannot tell a manager whether a van is
 * expensive because it works hard or expensive because it sits with the engine running -
 * and those lead to opposite decisions. The utilisation score answers that, so it belongs
 * on the same page rather than one tab away.
 *
 * Reached by clicking a VIN on any other view, or by typing one in. A search box rather
 * than a list, because a fleet of 100,000 vehicles has no useful "all vehicles" page.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';

import { api } from '../api/client';
import type { VehicleUtilisation } from '../api/types';
import {
  Badge,
  EmptyState,
  ErrorState,
  KpiCard,
  LoadingState,
  formatHours,
  formatMoney,
} from '../components/common';
import { IdleEventList } from '../components/IdleEventList';

/** A VIN is 17 characters and never contains I, O or Q (ISO 3779). */
const VIN_LENGTH = 17;

function bandTone(band: string): 'warn' | 'good' | 'info' | 'neutral' {
  if (band === 'low') return 'warn';
  if (band === 'abnormal') return 'info';
  if (band === 'high') return 'good';
  return 'neutral';
}

export function VehicleDetail({
  days,
  vin,
  onVinChange,
}: {
  days: number;
  vin: string | null;
  onVinChange: (next: string | null) => void;
}): ReactNode {
  return (
    <>
      <VinSearch vin={vin} onVinChange={onVinChange} />
      {vin === null ? (
        <EmptyState
          title="No vehicle selected"
          hint="Enter a VIN above, or click a vehicle on the leaderboard or utilisation view."
        />
      ) : (
        <VehicleBody vin={vin} days={days} />
      )}
    </>
  );
}

function VehicleBody({ vin, days }: { vin: string; days: number }): ReactNode {
  const vehicle = useQuery({
    queryKey: ['vehicle', vin],
    queryFn: () => api.vehicle(vin),
    // A mistyped VIN is a 404, and retrying will not change that.
    retry: false,
  });

  if (vehicle.isPending) return <LoadingState label={`Loading ${vin}…`} />;
  if (vehicle.isError) {
    return <ErrorState error={vehicle.error} onRetry={() => void vehicle.refetch()} />;
  }

  const v = vehicle.data;

  return (
    <>
      <section className="panel" aria-label="Vehicle identity">
        <h2 className="panel__title">
          <code className="vin">{v.vin}</code> &middot; {v.registration}
        </h2>
        <dl className="assumptions__list">
          <dt>Depot</dt>
          <dd>{v.fleet_name}</dd>
          <dt>Class</dt>
          <dd>{v.vehicle_class.replace(/_/g, ' ')}</dd>
          <dt>Powertrain</dt>
          <dd>
            {v.powertrain}
            {v.fuel_type && <span className="subtle"> · {v.fuel_type}</span>}
            {!v.burns_fuel && (
              <>
                {' '}
                <Badge tone="info">no idle fuel cost</Badge>
              </>
            )}
          </dd>
          <dt>Model year</dt>
          <dd>{v.model_year}</dd>
          <dt>Status</dt>
          <dd>
            {v.is_active ? (
              <Badge tone="good">active</Badge>
            ) : (
              <Badge tone="neutral">retired</Badge>
            )}
          </dd>
        </dl>
      </section>

      <div className="kpi-grid">
        <KpiCard
          label="Idle events"
          value={v.idle_event_count.toLocaleString()}
          hint={`over the last ${days} days`}
        />
        <KpiCard
          label="Idle time"
          value={formatHours(v.total_idle_hours)}
          tone={v.total_idle_hours > 0 ? 'warn' : 'neutral'}
        />
        <KpiCard
          label="Estimated idle cost"
          value={
            v.burns_fuel
              ? formatMoney(v.total_cost, v.currency)
              : 'not applicable'
          }
          tone={v.burns_fuel ? 'warn' : 'neutral'}
          hint={
            v.burns_fuel
              ? 'An estimate, not a measurement'
              : 'Electric: consumes no fuel at rest (D9)'
          }
        />
        <UtilisationKpi utilisation={v.utilisation} />
      </div>

      {v.utilisation && <UtilisationBreakdown utilisation={v.utilisation} />}

      <section className="panel" aria-label={`Idle events for ${v.vin}`}>
        <h2 className="panel__title">Idle events</h2>
        <IdleEventList
          vin={v.vin}
          days={days}
          pageSize={50}
          emptyHint={
            v.idle_event_count > 0
              ? `This vehicle has ${v.idle_event_count} idle events in total, but none in the last ${days} days. Widen the window.`
              : 'This vehicle has never idled beyond the threshold.'
          }
        />
      </section>
    </>
  );
}

/**
 * The utilisation score as a KPI.
 *
 * Never renders an unscored vehicle as zero. "We could not score it" and "it scored 0"
 * lead to opposite actions - one is a telematics unit to chase, the other is a van to
 * sell - so an absent score says so in words.
 */
function UtilisationKpi({
  utilisation,
}: {
  utilisation: VehicleUtilisation | null;
}): ReactNode {
  if (!utilisation) {
    return (
      <KpiCard
        label="Utilisation"
        value="not scored"
        hint="No usable telemetry in the window, or too few class peers for a baseline"
      />
    );
  }
  return (
    <KpiCard
      label={`Utilisation · ${utilisation.band}`}
      value={utilisation.score.toFixed(1)}
      tone={utilisation.band === 'low' ? 'warn' : utilisation.band === 'high' ? 'good' : 'neutral'}
      hint={`scored against ${utilisation.baseline_sample_size} class peers`}
    />
  );
}

/**
 * The three components behind the score, plus the assumptions.
 *
 * A single number is not enough to act on. A manager deciding whether to sell a van needs
 * to see that it was out every day but barely moved, which is a broken feed or a misuse
 * problem rather than an under-used asset.
 */
function UtilisationBreakdown({
  utilisation,
}: {
  utilisation: VehicleUtilisation;
}): ReactNode {
  const components = [
    { label: 'Active days', value: utilisation.active_day_component, weight: '40%' },
    { label: 'Moving hours', value: utilisation.moving_hours_component, weight: '35%' },
    { label: 'Distance', value: utilisation.distance_component, weight: '25%' },
  ];

  return (
    <section className="panel" aria-label="Utilisation breakdown">
      <h2 className="panel__title">
        Why it scores {utilisation.score.toFixed(1)}{' '}
        <Badge tone={bandTone(utilisation.band)}>{utilisation.band}</Badge>
      </h2>

      {utilisation.abnormal_reason && (
        <p className="notice notice--info">
          <strong>Its own signals disagree:</strong> {utilisation.abnormal_reason}. That is a
          data-quality or misuse signal rather than a level of use, so it is reported
          separately and never counted as low utilisation.
        </p>
      )}

      <dl className="assumptions__list">
        {components.map((component) => (
          <div key={component.label} style={{ display: 'contents' }}>
            <dt>
              {component.label} <span className="subtle">({component.weight})</span>
            </dt>
            <dd>
              <div
                className="bar"
                title={`${component.label}: ${Math.round(component.value * 100)}% of the class median`}
              >
                <div
                  className="bar__fill"
                  style={{ width: `${Math.min(Math.round(component.value * 100), 100)}%` }}
                />
                <span className="bar__text">{Math.round(component.value * 100)}%</span>
              </div>
            </dd>
          </div>
        ))}
        <dt>Idle share</dt>
        <dd>{Math.round(utilisation.idle_share * 100)}% of engine-on time</dd>
        <dt>Window</dt>
        <dd>
          {utilisation.window_days} days to {utilisation.window_end}
        </dd>
        <dt>Baseline</dt>
        <dd>
          {utilisation.baseline_sample_size} peers in the same vehicle class. A score from
          few peers is weaker than one from many, so this number is shown rather than
          assumed.
        </dd>
        <dt>Formula</dt>
        <dd>
          <code>{utilisation.formula_version}</code> &mdash; each component measured against
          the median for the same class, so a heavy truck is never judged by a city
          car&apos;s norms.
        </dd>
      </dl>
    </section>
  );
}

function VinSearch({
  vin,
  onVinChange,
}: {
  vin: string | null;
  onVinChange: (next: string | null) => void;
}): ReactNode {
  const [draft, setDraft] = useState(vin ?? '');
  const trimmed = draft.trim().toUpperCase();
  // Checked before the request, so an obviously wrong length gives an immediate answer
  // instead of a round trip and a 422.
  const valid = trimmed.length === VIN_LENGTH;

  function submit(event: FormEvent): void {
    event.preventDefault();
    if (valid) onVinChange(trimmed);
  }

  return (
    <form className="toolbar" onSubmit={submit}>
      <label className="field">
        <span className="field__label">VIN</span>
        <input
          className="select"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="17 characters"
          maxLength={VIN_LENGTH}
          size={20}
          aria-describedby="vin-hint"
        />
      </label>
      <button type="submit" className="button button--primary" disabled={!valid}>
        Look up
      </button>
      <span id="vin-hint" className="subtle small">
        {trimmed.length === 0
          ? 'A VIN is 17 characters.'
          : valid
            ? 'Ready.'
            : `${trimmed.length} of ${VIN_LENGTH} characters.`}
      </span>
    </form>
  );
}
