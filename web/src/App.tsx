/**
 * Application shell: navigation, the reporting window, and development sign-in.
 *
 * All five views from CLAUDE.md S14. The selected VIN lives here rather than inside
 * Vehicle Detail, so clicking a vehicle on the leaderboard or the utilisation view can
 * open it - which is what makes one fleet figure traceable to one vehicle and then to the
 * individual idle periods behind it.
 */

import { useState } from 'react';
import type { ReactNode } from 'react';

import { devLogin, hasAccessToken } from './api/client';
import { ErrorState } from './components/common';
import { Attention } from './pages/Attention';
import { FleetOverview } from './pages/FleetOverview';
import { IdleLeaderboard } from './pages/IdleLeaderboard';
import { Utilisation } from './pages/Utilisation';
import { VehicleDetail } from './pages/VehicleDetail';

type View = 'overview' | 'leaderboard' | 'utilisation' | 'attention' | 'vehicle';

const TABS: ReadonlyArray<{ id: View; label: string }> = [
  { id: 'overview', label: 'Fleet overview' },
  { id: 'leaderboard', label: 'Idle cost leaderboard' },
  { id: 'utilisation', label: 'Utilisation' },
  { id: 'attention', label: 'Needs attention' },
  { id: 'vehicle', label: 'Vehicle detail' },
];

const WINDOWS = [7, 30, 90] as const;

const ROLES = [
  { value: 'finance_manager', label: 'Finance manager' },
  { value: 'ops_manager', label: 'Operations manager' },
] as const;

const TENANTS = ['tenant-demo', 'tenant-second'] as const;

export function App(): ReactNode {
  const [view, setView] = useState<View>('overview');
  const [days, setDays] = useState<number>(30);
  const [selectedVin, setSelectedVin] = useState<string | null>(null);
  const [signedIn, setSignedIn] = useState(hasAccessToken());

  /** Open one vehicle from any other view, so a fleet figure stays traceable. */
  function openVehicle(vin: string | null): void {
    setSelectedVin(vin);
    if (vin !== null) setView('vehicle');
  }

  if (!signedIn) {
    return <DevSignIn onSignedIn={() => setSignedIn(true)} />;
  }

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1 className="app__title">Fleet Idle &amp; Utilisation Cost</h1>
          <p className="app__subtitle">
            All monetary figures are <strong>estimates</strong> from synthetic telemetry.
          </p>
        </div>

        <label className="field">
          <span className="field__label">Window</span>
          <select
            className="select"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
          >
            {WINDOWS.map((window) => (
              <option key={window} value={window}>
                Last {window} days
              </option>
            ))}
          </select>
        </label>
      </header>

      <nav className="tabs" aria-label="Views">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`tab ${view === tab.id ? 'is-active' : ''}`}
            aria-current={view === tab.id ? 'page' : undefined}
            onClick={() => setView(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <main className="app__main">
        {view === 'overview' && <FleetOverview days={days} />}
        {view === 'leaderboard' && <IdleLeaderboard days={days} onOpenVehicle={openVehicle} />}
        {view === 'utilisation' && <Utilisation onOpenVehicle={openVehicle} />}
        {view === 'attention' && <Attention onOpenVehicle={openVehicle} />}
        {view === 'vehicle' && (
          <VehicleDetail days={days} vin={selectedVin} onVinChange={setSelectedVin} />
        )}
      </main>

      <footer className="app__footer">
        <p>
          Synthetic data only. Independent academic exercise; Motorq is referenced as an
          industry example with no affiliation.
        </p>
      </footer>
    </div>
  );
}

/**
 * Development sign-in against the local OIDC stub.
 *
 * Deliberately not a real login screen. D4 is still open, so committing to a provider's
 * flow now would be exactly the invented decision CLAUDE.md S5 forbids. Picking a role
 * here also makes the location-masking difference demonstrable: finance sees coarsened
 * coordinates, operations sees precise ones.
 */
function DevSignIn({ onSignedIn }: { onSignedIn: () => void }): ReactNode {
  const [tenant, setTenant] = useState<string>(TENANTS[0]);
  const [role, setRole] = useState<string>(ROLES[0].value);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await devLogin(tenant, role);
      onSignedIn();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="signin">
      <div className="signin__card">
        <h1 className="app__title">Fleet Idle &amp; Utilisation Cost</h1>
        <p className="app__subtitle">
          Development sign-in. Tokens come from the local OIDC stub; no authentication
          provider has been chosen yet (decision D4).
        </p>

        <label className="field">
          <span className="field__label">Tenant</span>
          <select
            className="select"
            value={tenant}
            onChange={(event) => setTenant(event.target.value)}
          >
            {TENANTS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">Role</span>
          <select
            className="select"
            value={role}
            onChange={(event) => setRole(event.target.value)}
          >
            {ROLES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <p className="signin__hint">
          Finance roles receive coarsened coordinates; operations roles receive precise
          ones. Switching tenant demonstrates isolation: each sees only its own vehicles.
        </p>

        {error !== null && <ErrorState error={error} />}

        <button type="button" className="button button--primary" disabled={busy} onClick={() => void submit()}>
          {busy ? 'Signing in…' : 'Continue'}
        </button>
      </div>
    </div>
  );
}
