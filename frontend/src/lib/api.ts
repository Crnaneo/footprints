export class ApiError extends Error {
  constructor(message: string, public status: number, public details: unknown) { super(message); }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (response.status === 204) return undefined as T;
  let body: unknown;
  try { body = await response.json(); } catch { throw new ApiError('本地服务暂时没有响应，请检查应用是否已启动。', response.status, null); }
  if (!response.ok) {
    const payload = body as Record<string, unknown>;
    const detail = payload.detail;
    const nested = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail as Record<string, unknown> : null;
    const message = typeof payload.message === 'string' ? payload.message
      : typeof detail === 'string' ? detail : typeof nested?.message === 'string' ? nested.message
      : response.status === 422 ? '请检查输入的日期、坐标或 CSV 格式。' : '操作未完成，请稍后重试。';
    throw new ApiError(message, response.status, payload.details ?? nested?.details ?? detail ?? body);
  }
  return body as T;
}

export function postJson<T>(path: string, value: unknown, options?: Pick<RequestInit, 'signal'>) {
  return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value), ...options });
}
