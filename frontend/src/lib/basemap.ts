import type { StyleSpecification } from 'maplibre-gl';

const STYLE_URL = 'https://tiles.openfreemap.org/styles/dark';
let cached: StyleSpecification | null = null;
let pending: Promise<StyleSpecification> | null = null;

/** Share the base style across the main map and short-lived preview maps. */
export async function loadBasemapStyle(signal: AbortSignal): Promise<StyleSpecification> {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  if (!cached) {
    if (!pending) {
      pending = fetch(STYLE_URL, { signal: AbortSignal.timeout(15_000) })
        .then(async response => {
          if (!response.ok) throw new Error('Basemap unavailable');
          cached = await response.json() as StyleSpecification;
          return cached;
        }).finally(() => { pending = null; });
    }
    await pending;
  }
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  // Each map adds its own point/line source; never mutate the shared style.
  return structuredClone(cached!);
}
