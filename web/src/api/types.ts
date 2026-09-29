/**
 * API response types.
 *
 * Mirrors the FastAPI response models. Hand-written for now and generated from
 * `/openapi.json` before submission, per CLAUDE.md S7 ("shared API client generated
 * from OpenAPI") - a contract test will then keep them in step.
 *
 * Money is `string`, never `number`. JSON numbers are IEEE 754 doubles in JavaScript,
 * so parsing `"3733.5017"` into a float and summing it would accumulate error. Amounts
 * are formatted for display and never arithmetic'd in the browser (D11).
 */

/** Everything needed to reproduce a cost figure by hand (CLAUDE.md S14). */
export interface Assumptions {
  formula_version: string;
  burn_rate_lph: number | null;
  fuel_price_per_litre: string | null;
  price_id: string | null;
  idle_threshold_seconds: number;
  currency: string | null;
}

export interface FleetSummary {
  tenant_id: string;
  window_from: string;
  window_to: string;
  vehicle_count: number;
  vehicles_with_idle: number;
  idle_event_count: number;
  excessive_count: number;
  total_idle_hours: number;
  total_fuel_litres: number;
  /** Exact decimal as a string. Format it; do not parse it into a float. */
  total_cost: string;
  currency: string;
  open_alert_count: number;
  /** Vehicles with no fuel cost because they are electric (D9). */
  excluded_bev_count: number;
  /** True when the cost total covers fewer vehicles than it appears to. */
  cost_total_is_partial: boolean;
  is_estimate: boolean;
  assumptions: Assumptions;
}

export interface IdleEvent {
  idle_event_id: number;
  vin: string;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  duration_minutes: number;
  sample_count: number;
  /** How much unobserved time the segment bridged: a data-quality signal (D1). */
  max_gap_seconds: number;
  excessive: boolean;
  pto_active_any: boolean;
  start_lat: number | null;
  start_lon: number | null;
  /** True when coordinates were reduced in precision for this role (PDF S11). */
  location_coarsened: boolean;
  fuel_litres_est: number | null;
  cost_est: string | null;
  /** The device's own figure, for comparison against the estimate (D2). */
  fuel_litres_measured: number | null;
  /** True for an electric vehicle: no fuel cost exists, as distinct from zero. */
  bev_excluded: boolean;
  is_estimate: boolean;
  assumptions: Assumptions;
}

/** Keyset pagination envelope. No total count, by design - see PageMeta in the API. */
export interface PageMeta {
  next_cursor: string | null;
  page_size: number;
  returned: number;
  has_more: boolean;
}

export interface IdleEventPage {
  items: IdleEvent[];
  page: PageMeta;
}

export interface LeaderboardEntry {
  rank: number;
  vin: string;
  registration: string;
  vehicle_class: string;
  powertrain: string;
  fleet_name: string;
  idle_event_count: number;
  excessive_count: number;
  total_idle_hours: number;
  total_fuel_litres: number;
  total_cost: string;
  currency: string;
}

export interface LeaderboardResponse {
  items: LeaderboardEntry[];
  since: string;
  /** Electric vehicles cannot be ranked by fuel cost, and the API says so. */
  excludes_bev: boolean;
  is_estimate: boolean;
}

export interface TrendPoint {
  day: string;
  idle_hours: number;
  cost: string;
}

export interface TrendResponse {
  items: TrendPoint[];
  currency: string;
  is_estimate: boolean;
}

export interface VehicleDetail {
  vin: string;
  registration: string;
  tenant_id: string;
  fleet_name: string;
  vehicle_class: string;
  powertrain: string;
  fuel_type: string | null;
  model_year: number;
  is_active: boolean;
  /** False for electric: `total_cost` is then null by design, not by omission. */
  burns_fuel: boolean;
  idle_event_count: number;
  total_idle_hours: number;
  total_cost: string | null;
  currency: string | null;
}

/** RFC 7807 problem document (CLAUDE.md S10). */
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  correlation_id: string;
  instance?: string;
}
