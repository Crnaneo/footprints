/** Small batches retain individual point IDs without a feature object per point. */
interface PointFeature {
  id?: string | number;
  geometry: { type: string; coordinates?: unknown };
  properties: { pointDeltas?: unknown } | null;
}

export function nearestPointId(
  feature: PointFeature, click: { x: number; y: number },
  project: (coordinate: [number, number]) => { x: number; y: number }, worldLongitude: number,
): number | null {
  const first = Number(feature.id);
  if (!Number.isSafeInteger(first) || first <= 0) return null;
  if (feature.geometry.type === 'Point') return first;
  if (feature.geometry.type !== 'MultiPoint') return null;
  const coordinates = feature.geometry.coordinates as [number, number][];
  let deltas: unknown;
  try { deltas = JSON.parse(String(feature.properties?.pointDeltas)); } catch { return null; }
  if (!Array.isArray(coordinates) || !Array.isArray(deltas) || deltas.length !== coordinates.length - 1) return null;
  let id = first, closest = first, distance = Infinity;
  for (let i = 0; i < coordinates.length; i++) {
    if (i) {
      if (!Number.isSafeInteger(deltas[i - 1])) return null;
      id += deltas[i - 1];
      if (!Number.isSafeInteger(id) || id <= 0) return null;
    }
    const [lon, lat] = coordinates[i];
    const position = project([lon + Math.round((worldLongitude - lon) / 360) * 360, lat]);
    const squared = (position.x - click.x) ** 2 + (position.y - click.y) ** 2;
    if (squared < distance) { distance = squared; closest = id; }
  }
  return closest;
}

type TileResponse = { data: ArrayBuffer; cacheControl?: string; expires?: string };
interface TileJob {
  url: string; signal: AbortSignal; controller: AbortController; readyAt: number;
  resolve: (response: TileResponse) => void; reject: (error: unknown) => void; abort: () => void;
}

/** Delay briefly to discard transient zoom tiles; bound backend/decoder work. */
export class PointTileQueue {
  private pending: TileJob[] = [];
  private active = new Set<TileJob>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;
  constructor(private readonly concurrency = 2, private readonly delay = 90) {}

  request(url: string, signal: AbortSignal): Promise<TileResponse> {
    if (this.disposed || signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
    return new Promise((resolve, reject) => {
      const job: TileJob = {
        url, signal, controller: new AbortController(), readyAt: Date.now() + this.delay, resolve, reject,
        abort: () => {
          this.pending = this.pending.filter(candidate => candidate !== job);
          job.controller.abort();
          signal.removeEventListener('abort', job.abort);
          reject(new DOMException('Aborted', 'AbortError'));
          this.drain();
        },
      };
      signal.addEventListener('abort', job.abort, { once: true });
      this.pending.push(job);
      this.drain();
    });
  }

  private drain() {
    if (this.timer !== null) { clearTimeout(this.timer); this.timer = null; }
    if (this.disposed) return;
    while (this.active.size < this.concurrency && this.pending.length) {
      const job = this.pending[0];
      const wait = job.readyAt - Date.now();
      if (wait > 0) { this.timer = setTimeout(() => { this.timer = null; this.drain(); }, wait); return; }
      this.pending.shift(); this.active.add(job);
      void fetch(job.url, { signal: job.controller.signal }).then(async response => {
        if (!response.ok) throw new Error(`轨迹瓦片加载失败（${response.status}）`);
        const data = await response.arrayBuffer();
        if (!job.controller.signal.aborted) job.resolve({ data,
          cacheControl: response.headers.get('cache-control') || undefined,
          expires: response.headers.get('expires') || undefined });
      }).catch(job.reject).finally(() => {
        job.signal.removeEventListener('abort', job.abort);
        this.active.delete(job); this.drain();
      });
    }
  }

  dispose() {
    this.disposed = true;
    if (this.timer !== null) clearTimeout(this.timer);
    for (const job of [...this.pending, ...this.active]) job.abort();
    this.pending = [];
  }
}
