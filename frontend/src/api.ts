import type { components } from './api.generated';
export type Session = components['schemas']['SessionView'];
export type Provider = components['schemas']['ProviderView'];
export type Model = components['schemas']['ModelView'];
export type Conversation = components['schemas']['ConversationView'];
export type Message = components['schemas']['MessageView'];
export type Run = components['schemas']['ChatRunView'];
export type Memory = components['schemas']['MemoryView'];
export type Candidate = components['schemas']['CandidateView'];
export type Operation = components['schemas']['OperationView'];
let csrf = '';
export function setCsrf(value: string) { csrf = value; }
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function api<T>(url: string, method = 'GET', body?: unknown, options: {signal?: AbortSignal} = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, { method, credentials: 'same-origin', signal: options.signal,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  } catch { throw new ApiError(options.signal?.aborted ? '服务器响应超时，请重试连接' : '无法连接服务器，请检查网络后重试', 0); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && !url.includes('/auth/')) window.dispatchEvent(new Event('lingxi:unauthorized'));
    const fallback = response.status >= 500 ? `服务暂不可用（${response.status}），请稍后重试` : `请求失败（${response.status}），请检查输入`;
    throw new ApiError(typeof data.detail === 'string' ? data.detail : fallback, response.status);
  }
  return response.json();
}
export function download(url: string) { window.location.assign(url); }
