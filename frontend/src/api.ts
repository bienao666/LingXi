import type { components } from './api.generated';
export type Session = components['schemas']['SessionView'];
export type Provider = components['schemas']['ProviderView'];
export type ProviderInput = components['schemas']['ProviderInput'];
export type Model = components['schemas']['ModelView'];
export type ModelInput = components['schemas']['ModelInput'];
export type ProviderModelCatalog = components['schemas']['ProviderModelCatalog'];
export type ProxyConfig = components['schemas']['ProxyView'];
export type ProxyTestResult = components['schemas']['ProxyTestResult'];
export type Conversation = components['schemas']['ConversationView'];
export type ConversationPage = components['schemas']['ConversationPage'];
export type MessagePage = components['schemas']['MessagePage'];
export type Message = components['schemas']['MediaMessageView'];
export type MediaItem = components['schemas']['MediaView'];
export type MediaDetail = components['schemas']['MediaDetail'];
export type MediaPage = components['schemas']['MediaPage'];
export type Vision = components['schemas']['VisionView'];
export type Run = components['schemas']['ChatRunView'];
export type ChatAction = components['schemas']['ChatActionView'];
export type Memory = components['schemas']['MemoryView'];
export type Candidate = components['schemas']['CandidateView'];
export type Operation = components['schemas']['OperationView'];
export type AssistantProfile = components['schemas']['ProfileView'];
export type ProfileRevision = components['schemas']['RevisionView'];
export type SupportPreferences = Required<components['schemas']['SupportView']>;
export type EmotionObservation = components['schemas']['ObservationView'];
export type PersonaSelection = components['schemas']['SelectionView'];
export type Task = components['schemas']['TaskView'];
export type TaskDetail = components['schemas']['TaskDetail'];
export type TaskPage = components['schemas']['TaskPage'];
export type Reminder = components['schemas']['ReminderView'];
export type ReminderPage = components['schemas']['ReminderPage'];
export type Skill = components['schemas']['SkillView'];
export type ContactPreferences = Required<components['schemas']['PreferenceView']>;
export type ContactTargetOption = components['schemas']['TargetOption'];
export type Topic = components['schemas']['TopicView'];
export type TopicPage = components['schemas']['TopicPage'];
export type TopicSource = components['schemas']['TopicSourceView'];
export type Contact = components['schemas']['ContactView'];
export type ContactPage = components['schemas']['ContactPage'];
let csrf = '';
export function isContactControl(text:string) {return ['先别聊','暂停主动交流','以后别主动联系','直接告诉我结果','这个以后别问'].includes(text.trim().replace(/[。！!，,]+$/u,'').trim());}
export const supportCommands=['以后先听我说','以后多给建议','以后优先帮我行动','恢复平衡回应','关闭自动情绪线索','关闭情绪适配','清空当前感受'] as const;
export function isChatControl(text:string) {return isContactControl(text)||supportCommands.includes(text.trim().replace(/[。！!，,]+$/u,'').trim() as typeof supportCommands[number]);}
export function setCsrf(value: string) { csrf = value; }
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function api<T>(url: string, method = 'GET', body?: unknown, options: {signal?: AbortSignal; binary?: Blob; response?: 'blob'} = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, { method, credentials: 'same-origin', signal: options.signal,
      headers: { 'Content-Type': options.binary ? 'application/octet-stream' : 'application/json', 'X-CSRF-Token': csrf },
      ...(options.binary ? {body: options.binary} : body === undefined ? {} : { body: JSON.stringify(body) }) });
  } catch { throw new ApiError(options.signal?.aborted ? '服务器响应超时，请重试连接' : '无法连接服务器，请检查网络后重试', 0); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    // A failed password/TOTP proof must remain in its form. Read-only auth
    // queries prove that the browser session itself has expired or was revoked.
    if (response.status === 401 && (!url.includes('/auth/') || method === 'GET')) window.dispatchEvent(new window.Event('lingxi:unauthorized'));
    const fallback = response.status >= 500 ? `服务暂不可用（${response.status}），请稍后重试` : `请求失败（${response.status}），请检查输入`;
    throw new ApiError(typeof data.detail === 'string' ? data.detail : fallback, response.status);
  }
  const result=options.response==='blob' ? await response.blob() : await response.json();
  if (method!=='GET' && (url.startsWith('/api/contact-preferences') || url.startsWith('/api/proactive-contacts') || url.startsWith('/api/conversation-topics') || url==='/api/chat')) {
    window.dispatchEvent(new window.Event('lingxi:contact-preferences'));
  }
  return result;
}
export function download(url: string) { window.location.assign(url); }
