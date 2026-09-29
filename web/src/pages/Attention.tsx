/**
 * Attention: what needs acting on now (CLAUDE.md S14).
 *
 * Deliberately the shortest view. An alert list nobody can clear is an alert list
 * nobody reads, so this shows only unacknowledged alerts by default and states the
 * detection latency plainly rather than implying a speed it has not achieved.
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
  formatDateTime,
} from '../components/common';

const HEADERS = ['Raised', 'Vehicle', 'Alert', 'Detail', 'Detected in'] as const;

/** The D10 target. Shown next to the measured figure so the claim is checkable. */
const LATENCY_TARGET_SECONDS = 5;

function describeAlert(type: string): string {
  if (type === 'EXCESSIVE_IDLE_IN_PROGRESS') return 'Excessive idle in progress';
  if (type === 'IDLE_COST_SPIKE') return 'Idle cost spike';
  return type;
}

export function Attention({
  onOpenVehicle,
}: {
  onOpenVehicle: (vin: string) => void;
}): ReactNode {
  const [unacknowledgedOnly, setUnacknowledgedOnly] = useState(true);

  const alerts = useQuery({
    queryKey: ['alerts', unacknowledgedOnly],
    queryFn: () => api.alerts({ limit: 100, unacknowledgedOnly }),
  });

  if (alerts.isPending) return <LoadingState label="Loading alerts…" />;
  if (alerts.isError) {
    return <ErrorState error={alerts.error} onRetry={() => void alerts.refetch()} />;
  }

  const data = alerts.data;

  return (
    <>
      <div className="toolbar">
        <label className="field field--inline">
          <input
            type="checkbox"
            checked={unacknowledgedOnly}
            onChange={(event) => setUnacknowledgedOnly(event.target.checked)}
          />
          <span>Unacknowledged only</span>
        </label>

        {data.max_processing_latency_seconds !== null && (
          <p className="notice notice--inline">
            Slowest detection here:{' '}
            <strong>{data.max_processing_latency_seconds.toFixed(2)}s</strong> against a{' '}
            {LATENCY_TARGET_SECONDS}s target.
          </p>
        )}
      </div>

      {data.contains_backfill && (
        <p className="notice notice--info">
          Some of these alerts come from <strong>replayed history</strong>, not live
          telemetry. Detection time below measures how long the processor took once it
          received each event. The wall-clock gap back to the original event is the age
          of the data, not the speed of the system, so it is not shown as latency.
        </p>
      )}

      {data.items.length === 0 ? (
        <EmptyState
          title={unacknowledgedOnly ? 'Nothing needs attention' : 'No alerts recorded'}
          hint={
            unacknowledgedOnly
              ? 'No unacknowledged alerts. Untick the filter above to see the full history.'
              : 'No vehicle has crossed the excessive-idle threshold in the retained window.'
          }
        />
      ) : (
        <DataTable
          caption={`${data.items.length} alert${data.items.length === 1 ? '' : 's'}`}
          headers={HEADERS}
        >
          {data.items.map((alert) => {
            const idleSeconds = Number(alert.detail.idle_seconds ?? 0);
            const threshold = Number(alert.detail.threshold_seconds ?? 0);
            return (
              <tr key={alert.alert_id}>
                <td>{formatDateTime(alert.triggering_event_ts)}</td>
                <td>
                  <button
                    type="button"
                    className="link"
                    onClick={() => onOpenVehicle(alert.vin)}
                  >
                    <code className="vin">{alert.vin}</code>
                  </button>
                  <span className="subtle"> {alert.registration}</span>
                </td>
                <td>
                  <Badge tone={alert.severity === 'critical' ? 'warn' : 'info'}>
                    {describeAlert(alert.alert_type)}
                  </Badge>
                </td>
                <td className="small">
                  {idleSeconds > 0 ? (
                    <>
                      Idling {Math.round(idleSeconds / 60)} min
                      {threshold > 0 && (
                        <span className="subtle">
                          {' '}
                          (threshold {Math.round(threshold / 60)} min)
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="subtle">—</span>
                  )}
                </td>
                <td className="num">
                  {alert.processing_latency_seconds !== null ? (
                    <span
                      className={
                        alert.processing_latency_seconds > LATENCY_TARGET_SECONDS
                          ? 'over-target'
                          : undefined
                      }
                    >
                      {alert.processing_latency_seconds.toFixed(2)}s
                    </span>
                  ) : (
                    <span className="subtle">not recorded</span>
                  )}
                </td>
              </tr>
            );
          })}
        </DataTable>
      )}
    </>
  );
}
