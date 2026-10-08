import { api, postJson } from './api';

export interface LookupProgress { quarter: string; received_bytes: number; total_bytes: number; retry_after?: number; status_path: string }

export function lookupProgressText(progress: LookupProgress): string {
  const mb = (bytes: number) => (bytes / 1024 / 1024).toFixed(0);
  return `首次准备 ${progress.quarter} 历史航班索引：${mb(progress.received_bytes)} / ${mb(progress.total_bytes)} MB。完成后自动继续，后续查询复用本地缓存。`;
}

function pause(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException('查询已取消', 'AbortError')); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, 2000);
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
  });
}

export async function postFlightLookup<T>(path: string, body: unknown, onProgress: (message: string) => void, signal?: AbortSignal): Promise<T> {
  let preparationPath: string | null = null;
  try {
  for (let attempt = 0; attempt < 900; attempt++) {
    signal?.throwIfAborted();
    const response = await postJson<T | { lookup_pending: LookupProgress }>(path, body, signal ? { signal } : undefined);
    if (response && typeof response === 'object' && 'lookup_pending' in response) {
      let pending: LookupProgress | null = response.lookup_pending;
      for (let poll = 0; pending && poll < 900; poll++) {
        preparationPath = pending.status_path;
        onProgress(lookupProgressText(pending));
        await pause(signal);
        const state: { lookup_pending?: LookupProgress; ready?: boolean } = signal ? await api(pending.status_path, { signal }) : await api(pending.status_path);
        pending = state.lookup_pending || null;
      }
      if (pending) throw new Error('历史索引仍在准备，请稍后重试；下载缓存已保留。');
      preparationPath = null;
      onProgress('历史索引已准备，正在查询当日航班…');
      continue;
    }
    signal?.throwIfAborted();
    return response as T;
  }
  throw new Error('历史索引仍在准备，请稍后重试；已下载的数据会保留。');
  } catch (error) {
    if (signal?.aborted && preparationPath) {
      const name = new URL(preparationPath, 'http://localhost').searchParams.get('name');
      if (name) await postJson('/api/flight/schedules/cancel', { name });
    }
    throw error;
  }
}
