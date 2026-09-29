/**
 * The single API client. Every request in the app goes through here.
 *
 * CLAUDE.md S7 forbids per-page fetch logic, and the reason shows up immediately: the
 * bearer token, the correlation id, the problem-document handling and the timeout are
 * all cross-cutting. Duplicating them per page means one page eventually forgets one.
 */

import type {
  FleetSummary,
  IdleEventPage,
  LeaderboardResponse,
  Problem,
  TrendResponse,
  VehicleDetail,
} from './types';

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
const OIDC_URL: string = import.meta.env.VITE_OIDC_BASE_URL ?? 'http://localhost:8081';

/** Every request is bounded: without a timeout a hung API means a hung, silent UI. */
const REQUEST_TIMEOUT_MS = 15_000;

/**
 * An API failure carrying the server's problem document.
 *
 * The correlation id is kept and displayed, so a user reporting a failure gives us the
 * exact request rather than "it broke".
 */
export class ApiError extends Error {
  readonly status: number;
  readonly correlationId: string | null;
  readonly problemType: string | null;

  constructor(message: string, status: number, problem?: Problem) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.correlationId = problem?.correlation_id ?? null;
    this.problemType = problem?.type ?? null;
  }

  /** True when retrying could plausibly succeed. A 4xx will not fix itself. */
  get isRetryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

/**
 * Token storage.
 *
 * In memory, not localStorage: a token in localStorage is readable by any script on the
 * origin, so one XSS becomes a stolen session. The cost is re-authenticating on reload,
 * which for a development stub is nothing. The real flow depends on D4 and is not
 * decided yet, so this deliberately does not commit to one (CLAUDE.md S14).
 */
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function hasAccessToken(): boolean {
  return accessToken !== null;
}

/**
 * Obtain a token from the local development OIDC stub.
 *
 * Only for local development. A real deployment redirects to the configured provider;
 * this exists so the dashboard is usable before D4 is settled.
 */
export async function devLogin(tenantId: string, role: string): Promise<void> {
  const body = new URLSearchParams({ tenant_id: tenantId, role });
  const response = await fetch(`${OIDC_URL}/token`, { method: 'POST', body });
  if (!response.ok) {
    throw new ApiError(`development login failed (${response.status})`, response.status);
  }
  const payload = (await response.json()) as { access_token: string };
  setAccessToken(payload.access_token);
}

function newCorrelationId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `web-${Date.now()}-${Math.random()}`;
}

async function request<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const url = new URL(`${BASE_URL}${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== '') {
      url.searchParams.set(key, String(value));
    }
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      headers: {
        Accept: 'application/json',
        'X-Correlation-Id': newCorrelationId(),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      signal: controller.signal,
    });

    if (!response.ok) {
      // The API answers failures as problem+json, so the real title and correlation id
      // are available rather than a generic "request failed".
      let problem: Problem | undefined;
      try {
        problem = (await response.json()) as Problem;
      } catch {
        problem = undefined;
      }
      throw new ApiError(
        problem?.title ?? `Request failed with status ${response.status}`,
        response.status,
        problem,
      );
    }

    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      // Status 0: distinguishes "never reached the server" from a server error, which
      // matters because only one of them is the user's network.
      throw new ApiError('The request timed out', 0);
    }
    throw new ApiError('Could not reach the API', 0);
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  fleetSummary: (days: number) => request<FleetSummary>('/api/v1/fleet/summary', { days }),

  fleetTrend: (days: number) => request<TrendResponse>('/api/v1/fleet/trend', { days }),

  idleEvents: (options: {
    days: number;
    pageSize?: number;
    cursor?: string;
    vin?: string;
    excessiveOnly?: boolean;
  }) =>
    request<IdleEventPage>('/api/v1/idle-events', {
      days: options.days,
      page_size: options.pageSize,
      cursor: options.cursor,
      vin: options.vin,
      excessive_only: options.excessiveOnly,
    }),

  leaderboard: (days: number, limit: number) =>
    request<LeaderboardResponse>('/api/v1/leaderboard/idle-cost', { days, limit }),

  vehicle: (vin: string) => request<VehicleDetail>(`/api/v1/vehicles/${vin}`),

  ready: () => request<{ status: string; postgres: boolean; redis: boolean }>('/readyz'),
};
