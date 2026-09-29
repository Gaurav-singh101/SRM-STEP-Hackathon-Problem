/**
 * Daily idle cost and hours.
 *
 * In its own module so it can be lazy-loaded. Recharts is ~530 kB minified, larger than
 * everything else in the app combined, and only this one view needs it - so a user who
 * opens the leaderboard should never download it.
 */

import type { ReactNode } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import type { TrendResponse } from '../api/types';

export default function IdleTrendChart({ trend }: { trend: TrendResponse }): ReactNode {
  const data = trend.items.map((point) => ({
    day: point.day,
    hours: point.idle_hours,
    // Parsed for plotting only. Every figure a user reads is formatted from the exact
    // string, never from this number (D11).
    cost: Number(point.cost),
  }));

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
        <XAxis dataKey="day" tick={{ fontSize: 12 }} stroke="var(--muted)" />
        <YAxis
          yAxisId="cost"
          tick={{ fontSize: 12 }}
          stroke="var(--muted)"
          label={{
            value: `Estimated cost (${trend.currency})`,
            angle: -90,
            position: 'insideLeft',
            style: { fontSize: 11, fill: 'var(--muted)' },
          }}
        />
        <YAxis
          yAxisId="hours"
          orientation="right"
          tick={{ fontSize: 12 }}
          stroke="var(--muted)"
        />
        <Tooltip
          contentStyle={{
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            fontSize: 13,
          }}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Line
          yAxisId="cost"
          type="monotone"
          dataKey="cost"
          name={`Estimated cost (${trend.currency})`}
          stroke="var(--accent)"
          strokeWidth={2}
          dot={false}
        />
        <Line
          yAxisId="hours"
          type="monotone"
          dataKey="hours"
          name="Idle hours"
          stroke="var(--accent-2)"
          strokeWidth={2}
          strokeDasharray="4 3"
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
