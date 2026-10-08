import { afterEach, expect, it, vi } from 'vitest';
import { postFlightLookup, lookupProgressText } from './flightLookup';
import { api, postJson } from './api';
vi.mock('./api', () => ({ api: vi.fn(), postJson: vi.fn() }));
afterEach(() => { vi.useRealTimers(); vi.resetAllMocks(); });

it('polls only preparation status while a quarter downloads, then retries the lookup', async () => {
  vi.useFakeTimers();
  const progress = { quarter: '2026 Q1', received_bytes: 1, total_bytes: 840340093, status_path: '/api/flight/schedules/status?name=test' };
  vi.mocked(postJson).mockResolvedValueOnce({ lookup_pending: progress }).mockResolvedValueOnce({ trip: { points: [] } });
  vi.mocked(api).mockResolvedValueOnce({ lookup_pending: { ...progress, received_bytes: 500000000 } }).mockResolvedValueOnce({ ready: true });
  const messages = vi.fn();
  const result = postFlightLookup('/api/preview/flight', { flight_number: 'UA123', travel_date: '2026-02-08' }, messages);
  await vi.runAllTimersAsync();
  expect(await result).toEqual({ trip: { points: [] } });
  expect(postJson).toHaveBeenCalledTimes(2);
  expect(api).toHaveBeenCalledTimes(2);
  expect(api).toHaveBeenCalledWith(progress.status_path);
  expect(messages).toHaveBeenCalledWith(expect.stringContaining('477 / 801 MB'));
});

it('returns an immediate lookup without polling', async () => {
  vi.mocked(postJson).mockResolvedValueOnce({ trip: { points: [] } });
  await expect(postFlightLookup('/api/preview/flight', {}, vi.fn())).resolves.toEqual({ trip: { points: [] } });
  expect(api).not.toHaveBeenCalled();
  expect(lookupProgressText({ quarter:'2024 Q1', received_bytes:0,total_bytes:1048576,status_path:'/' })).toContain('0 / 1 MB');
});

it('stops server downloading and polling when the lookup is cancelled', async () => {
  vi.useFakeTimers();
  const progress = { quarter: '2025 Q4', received_bytes: 1, total_bytes: 900, status_path: '/api/flight/schedules/status?name=2025_Q4.parquet' };
  vi.mocked(postJson).mockResolvedValueOnce({ lookup_pending: progress }).mockResolvedValueOnce({ cancelled: true });
  const controller = new AbortController();
  const result = postFlightLookup('/api/preview/flight', {}, vi.fn(), controller.signal);
  const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
  await Promise.resolve(); controller.abort();
  await rejected;
  expect(postJson).toHaveBeenLastCalledWith('/api/flight/schedules/cancel', { name: '2025_Q4.parquet' });
  await vi.runAllTimersAsync();
  expect(api).not.toHaveBeenCalled();
});
