import { afterEach, expect, test, vi } from 'vitest';
import { nearestPointId, PointTileQueue } from './pointTiles';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test('packed point picking uses each original ID including signed gaps and duplicates', () => {
  const feature = { id: 100, properties: { pointDeltas: '[9,-3,1000]' },
    geometry: { type: 'MultiPoint', coordinates: [[1, 0], [2, 0], [3, 0], [3, 0]] } };
  const project = ([x, y]: [number, number]) => ({ x, y });
  expect(nearestPointId(feature, { x: 2, y: 0 }, project, 0)).toBe(109);
  expect(nearestPointId(feature, { x: 3, y: 0 }, project, 0)).toBe(106);
  expect(nearestPointId({ ...feature, id: 7, geometry: { type: 'Point', coordinates: [2, 0] } }, { x: 2, y: 0 }, project, 0)).toBe(7);
  expect(nearestPointId({ ...feature, properties: { pointDeltas: '[1]' } }, { x: 2, y: 0 }, project, 0)).toBeNull();
});

test('packed point picking follows the clicked world copy at the dateline', () => {
  const project = ([x, y]: [number, number]) => ({ x, y });
  const feature = { id: 30, properties: { pointDeltas: '[5]' },
    geometry: { type: 'MultiPoint', coordinates: [[179, 0], [-179, 0]] } };
  expect(nearestPointId(feature, { x: 181, y: 0 }, project, 181)).toBe(35);
  expect(nearestPointId(feature, { x: -181, y: 0 }, project, -181)).toBe(30);
});

test('transient zoom requests are aborted before reaching the server', async () => {
  vi.useFakeTimers(); const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  const queue = new PointTileQueue(); const controller = new AbortController();
  const request = queue.request('/old-zoom', controller.signal);
  const rejected = expect(request).rejects.toHaveProperty('name', 'AbortError');
  controller.abort(); await rejected; await vi.runAllTimersAsync();
  expect(fetcher).not.toHaveBeenCalled(); queue.dispose();
});

test('tile requests keep a concurrency limit and release slots after failures', async () => {
  vi.useFakeTimers();
  const responses: Array<(response: unknown) => void> = [];
  const fetcher = vi.fn(() => new Promise(resolve => responses.push(resolve)));
  vi.stubGlobal('fetch', fetcher);
  const queue = new PointTileQueue(2, 90);
  const requests = [0, 1, 2].map(i => queue.request('/tile/' + i, new AbortController().signal));
  const failure = expect(requests[0]).rejects.toThrow('503');
  await vi.advanceTimersByTimeAsync(89); expect(fetcher).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1); expect(fetcher).toHaveBeenCalledTimes(2);
  responses[0]({ ok: false, status: 503 }); await failure;
  await vi.advanceTimersByTimeAsync(0); expect(fetcher).toHaveBeenCalledTimes(3);
  const response = { ok: true, headers: new Headers({ 'cache-control': 'private, no-cache' }), arrayBuffer: async () => new ArrayBuffer(8) };
  responses[1](response); responses[2](response);
  for (const request of requests.slice(1)) expect((await request).data.byteLength).toBe(8);
  queue.dispose();
});

test('disposing the map aborts active fetches and queued jobs', async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn((_url, options: RequestInit) => new Promise((_resolve, reject) =>
    options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))));
  vi.stubGlobal('fetch', fetcher);
  const queue = new PointTileQueue(1);
  const first = expect(queue.request('/active', new AbortController().signal)).rejects.toHaveProperty('name', 'AbortError');
  const second = expect(queue.request('/queued', new AbortController().signal)).rejects.toHaveProperty('name', 'AbortError');
  await vi.advanceTimersByTimeAsync(90); expect(fetcher).toHaveBeenCalledTimes(1);
  queue.dispose(); await Promise.all([first, second]);
  await vi.runAllTimersAsync(); expect(fetcher).toHaveBeenCalledTimes(1);
  await expect(queue.request('/disposed', new AbortController().signal)).rejects.toHaveProperty('name', 'AbortError');
});
