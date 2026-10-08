/** Format a UTC observation using the airport's IANA zone, including DST. */
export function formatAirportTime(value?: string | null, timeZone?: string | null): string | null {
  if (!value) return null;
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
  if (!Number.isFinite(date.getTime())) return null;
  let zone = timeZone || 'UTC';
  let known = !!timeZone;
  try { new Intl.DateTimeFormat('zh-CN', { timeZone: zone }); }
  catch { zone = 'UTC'; known = false; }
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'longOffset',
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find(p => p.type === type)?.value || '';
  const offset = part('timeZoneName').replace('GMT', 'UTC').replace(/^UTC$/, 'UTC+00:00');
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')} ${offset}${known ? '' : '（机场时区未知）'}`;
}
