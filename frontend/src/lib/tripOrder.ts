import type { FlightInfo, Trip } from '../types';

function departure(trip: Trip): number | null {
  const info = trip.metadata?.flight as FlightInfo | undefined;
  const recovery = trip.metadata?.local_recovery as { start?: string } | undefined;
  for (const value of [info?.takeoff_time, recovery?.start, trip.points?.find(p => p.timestamp)?.timestamp]) {
    if (!value) continue;
    const instant = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
    if (Number.isFinite(instant)) return instant;
  }
  const batch = trip.metadata?.batch_import as { scheduled_departure_local?: string } | undefined;
  const clock = batch?.scheduled_departure_local;
  const zone = info?.departure_timezone || trip.metadata?.date_timezone;
  if (clock && /^\d{2}:\d{2}$/.test(clock) && typeof zone === 'string') {
    const local = Date.parse(`${trip.travel_date}T${clock}:00Z`);
    let instant = local;
    try {
      const format = new Intl.DateTimeFormat('sv-SE', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
      for (let i = 0; i < 2; i++) {
        const parts = format.formatToParts(instant);
        const p = (type: Intl.DateTimeFormatPartTypes) => parts.find(v => v.type === type)?.value;
        const rendered = Date.parse(`${p('year')}-${p('month')}-${p('day')}T${p('hour')}:${p('minute')}:${p('second')}Z`);
        instant += local - rendered;
      }
      if (Number.isFinite(instant)) return instant;
    } catch { /* Unknown airport timezone leaves the departure unspecified. */ }
  }
  return null;
}

/** Within a departure date, later flights are always displayed above earlier flights. */
export function compareTripDates(a: Trip, b: Trip, oldest = false): number {
  const day = oldest ? a.travel_date.localeCompare(b.travel_date) : b.travel_date.localeCompare(a.travel_date);
  if (day) return day;
  const at = departure(a), bt = departure(b);
  if (at !== null || bt !== null) return (bt ?? -Infinity) - (at ?? -Infinity);
  return b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id);
}
