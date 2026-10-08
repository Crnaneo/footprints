import { afterEach, beforeEach, expect, test, vi } from 'vitest';

beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllGlobals());

const style = { version: 8, sources: {}, layers: [{ id: 'background', type: 'background' }] };

test('main map and previews share one request and receive independent styles', async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => style }));
  vi.stubGlobal('fetch', fetch);
  const { loadBasemapStyle } = await import('./basemap');
  const [main, preview] = await Promise.all([
    loadBasemapStyle(new AbortController().signal), loadBasemapStyle(new AbortController().signal),
  ]);
  main.layers.push({ id: 'track', type: 'background' });
  expect(preview.layers).toHaveLength(1);
  expect((await loadBasemapStyle(new AbortController().signal)).layers).toHaveLength(1);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('failed styles can be retried, and aborted previews never receive a style', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ ok: true, json: async () => style }));
  const { loadBasemapStyle } = await import('./basemap');
  await expect(loadBasemapStyle(new AbortController().signal)).rejects.toThrow('offline');
  await expect(loadBasemapStyle(new AbortController().signal)).resolves.toHaveProperty('version', 8);
  const controller = new AbortController(); controller.abort();
  await expect(loadBasemapStyle(controller.signal)).rejects.toHaveProperty('name', 'AbortError');
});
