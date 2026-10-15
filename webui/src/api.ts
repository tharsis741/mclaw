import type {
  Bootstrap,
  Session,
  SessionEvent,
  SetupRun,
  SetupStatus,
  SkillSummary,
  SkillDetail,
  SkillDraft,
  SkillFile,
  MemoryDocument,
  Diagnostics,
  ScheduleSpec,
  ScheduleForm,
  ScheduledJob,
  ScheduleRun,
  ScheduleTarget,
  SchedulePairing,
  VoiceStatus,
  FileReference,
  FilePreview,
  FileBrowse,
  ProviderCatalog,
  ProviderForm,
  ModelLibrarySnapshot,
  ModelLibraryStatus,
  ToolCatalog,
  ToolForm,
  ConfigurationSnapshot,
  ConfigurationValue,
  ScheduleDefaults,
  DeviceSnapshot,
  TaskNotificationFeed,
} from './types';

import { applySnapshotChanges, type SnapshotChange } from './streaming';

const ROOT = '/api/v1';
export const devicesApi = {
  status: () => request<DeviceSnapshot>('/devices'),
  configure: (enabled: boolean) => request<DeviceSnapshot>('/devices', 'PATCH', { enabled }),
  act: (action: 'discover' | 'pair' | 'unpair', deviceId = '') =>
    request<DeviceSnapshot>(`/devices/${action}`, 'POST', {
      device_id: deviceId,
      confirmed: action !== 'discover',
    }),
};
export const toolsApi = {
  catalog: () => request<ToolCatalog>('/tools'),
  save: (id: string, form: ToolForm) =>
    request<ToolCatalog>(`/tools/${encodeURIComponent(id)}`, 'PUT', form),
};
const TOKEN_KEY = 'mclaw.connection.token';
const CLIENT_KEY = 'mclaw.connection.client';

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public current?: unknown,
  ) {
    super(message);
  }
}

let connectionReady: Promise<void> | undefined;

export function subscribeBrowserLaunch(onLaunch: () => void): () => void {
  // A native browser may reuse this page through a fragment-only navigation.
  // Reload so a new service credential also replaces existing event connections.
  const receive = () => {
    if (new URLSearchParams(window.location.hash.slice(1)).get('token')) onLaunch();
  };
  window.addEventListener('hashchange', receive);
  return () => window.removeEventListener('hashchange', receive);
}

export function initializeConnection(): Promise<void> {
  if (connectionReady) return connectionReady;
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get('token');
  if (token) {
    sessionStorage.setItem(TOKEN_KEY, token);
    fragment.delete('token');
    const rest = fragment.toString();
    window.history.replaceState(
      null,
      '',
      `${window.location.pathname}${window.location.search}${rest ? `#${rest}` : ''}`,
    );
  }
  if (!sessionStorage.getItem(CLIENT_KEY)) sessionStorage.setItem(CLIENT_KEY, crypto.randomUUID());
  // Duplicating a tab clones sessionStorage. Ask live pages before reusing its controller ID.
  connectionReady = new Promise((resolve) => {
    const instance = crypto.randomUUID();
    let active = false;
    let collision = false;
    const candidate = sessionStorage.getItem(CLIENT_KEY)!;
    const channel =
      typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('mclaw.controllers.v1');
    type Signal = { type: 'probe' | 'occupied'; client: string; instance: string; target?: string };
    const post = (signal: Signal) => {
      if (channel) channel.postMessage(signal);
      else {
        try {
          localStorage.setItem(
            'mclaw.controller.signal',
            JSON.stringify({ ...signal, at: Date.now() }),
          );
        } catch {
          /* Browser storage may be restricted. */
        }
      }
    };
    const receive = (signal: Signal) => {
      if (signal.instance === instance || signal.client !== sessionStorage.getItem(CLIENT_KEY))
        return;
      if (signal.type === 'probe' && (active || instance < signal.instance))
        post({ type: 'occupied', client: signal.client, instance, target: signal.instance });
      if (signal.type === 'occupied' && signal.target === instance) collision = true;
    };
    if (channel) channel.onmessage = (event) => receive(event.data as Signal);
    else
      window.addEventListener('storage', (event) => {
        if (event.key === 'mclaw.controller.signal' && event.newValue) {
          try {
            receive(JSON.parse(event.newValue));
          } catch {
            /* Ignore invalid storage events. */
          }
        }
      });
    window.addEventListener('pagehide', (event) => {
      if (!event.persisted) channel?.close();
    });
    post({ type: 'probe', client: candidate, instance });
    setTimeout(() => {
      if (collision) sessionStorage.setItem(CLIENT_KEY, crypto.randomUUID());
      active = true;
      resolve();
    }, 180);
  });
  return connectionReady;
}

export async function request<T>(
  path: string,
  method = 'GET',
  data?: unknown,
  extraHeaders?: Record<string, string>,
  signal?: AbortSignal,
): Promise<T> {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const headers: Record<string, string> = {
    'X-MClaw-Client': sessionStorage.getItem(CLIENT_KEY) ?? '',
    ...extraHeaders,
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (data !== undefined && !(data instanceof Blob)) headers['Content-Type'] = 'application/json';
  let response: Response;
  try {
    response = await fetch(`${ROOT}${path}`, {
      method,
      headers,
      body: data === undefined ? undefined : data instanceof Blob ? data : JSON.stringify(data),
      credentials: 'same-origin',
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError('disconnected', '无法连接本机 M-Claw 服务，请检查服务是否正在运行。', 0);
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new ApiError(
      payload.error?.code ?? 'request_failed',
      payload.error?.message ?? `请求失败（${response.status}）`,
      response.status,
      payload.error?.current,
    );
  return payload as T;
}

export const api = {
  health: () => request<{ status: string }>('/health'),
  stopService: () => request<{ status: string }>('/service/stop', 'POST', {}),
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  sessions: async (options: { q?: string; offset?: number; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (options.q) query.set('q', options.q);
    if (options.offset) query.set('offset', String(options.offset));
    if (options.limit) query.set('limit', String(options.limit));
    const result = await request<Session[] | { sessions: Session[] }>(
      `/sessions${query.size ? `?${query}` : ''}`,
    );
    return Array.isArray(result) ? result : result.sessions;
  },
  session: (id: string) => request<Session>(`/sessions/${encodeURIComponent(id)}`),
  createSession: (data: { workspace?: string; model?: string }) =>
    request<Session>('/sessions', 'POST', data),
  updateSession: (
    id: string,
    data: Partial<
      Pick<
        Session,
        | 'title'
        | 'model'
        | 'draft'
        | 'draft_skills'
        | 'draft_attachments'
        | 'draft_devices'
        | 'workspace'
      >
    >,
  ) => request<Session>(`/sessions/${encodeURIComponent(id)}`, 'PATCH', data),
  exportSession: (id: string) => request<unknown>(`/sessions/${encodeURIComponent(id)}/export`),
  deleteSession: (id: string) =>
    request<{ deleted: boolean; id: string }>(`/sessions/${encodeURIComponent(id)}`, 'DELETE'),
  submit: (
    id: string,
    content: string,
    mode: 'queue' | 'steer',
    request_id: string,
    skills?: string[],
    attachments?: string[],
    devices?: string[],
  ) =>
    request<Session>(`/sessions/${encodeURIComponent(id)}/messages`, 'POST', {
      content,
      mode,
      request_id,
      skills,
      attachments,
      devices,
    }),
  stop: (id: string) => request<Session>(`/sessions/${encodeURIComponent(id)}/stop`, 'POST', {}),
  reply: (id: string, request_id: string, value: unknown) =>
    request<Session>(`/sessions/${encodeURIComponent(id)}/reply`, 'POST', { request_id, value }),
  resume: (
    id: string,
    content?: string,
    request_id?: string,
    skills?: string[],
    attachments?: string[],
  ) =>
    request<Session>(`/sessions/${encodeURIComponent(id)}/resume`, 'POST', {
      content,
      request_id,
      skills,
      attachments,
    }),
  queue: (
    id: string,
    items: { id: string; content: string; skills?: string[]; attachments?: string[] }[],
  ) => request<Session>(`/sessions/${encodeURIComponent(id)}/queue`, 'PATCH', { items }),
  directories: (path?: string) =>
    request<{ path: string; parent: string; directories: { name: string; path: string }[] }>(
      `/directories${path ? `?path=${encodeURIComponent(path)}` : ''}`,
    ),
  settings: (output_root: string) =>
    request<{ output_root: string }>('/settings', 'PATCH', { output_root }),
  setupStatus: () => request<SetupStatus>('/setup'),
  setupStart: () => request<SetupRun>('/setup', 'POST', {}),
  setupRun: (id: string) => request<SetupRun>(`/setup/${encodeURIComponent(id)}`),
  setupSubmit: (id: string, prompt_id: string, value: string | string[]) =>
    request<SetupRun>(`/setup/${encodeURIComponent(id)}/submit`, 'POST', { prompt_id, value }),
  setupCancel: (id: string) =>
    request<SetupRun>(`/setup/${encodeURIComponent(id)}/cancel`, 'POST', {}),
};

export const taskNotificationsApi = {
  list: () => request<TaskNotificationFeed>('/task-notifications'),
  acknowledge: (ids: string[], action: 'dismiss' | 'read') =>
    request<TaskNotificationFeed>('/task-notifications', 'POST', { ids, action }),
};

export const providersApi = {
  list: (signal?: AbortSignal) =>
    request<ProviderCatalog>('/providers', 'GET', undefined, undefined, signal),
  save: (data: ProviderForm) => request<ProviderCatalog>('/providers', 'POST', data),
  models: (data: ProviderForm, signal?: AbortSignal) =>
    request<{ models: string[] }>('/providers/models', 'POST', data, undefined, signal),
  library: (query: string, provider: string, offset: number, signal?: AbortSignal) =>
    request<ModelLibrarySnapshot>(
      `/providers/library?${new URLSearchParams({ q: query, provider, offset: String(offset) })}`,
      'GET',
      undefined,
      undefined,
      signal,
    ),
  refreshLibrary: (signal?: AbortSignal) =>
    request<ModelLibraryStatus>('/providers/library', 'POST', {}, undefined, signal),
};

export const skillsApi = {
  list: (q = '') =>
    request<{ skills: SkillSummary[] }>(`/skills${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  detail: (name: string) => request<SkillDetail>(`/skills/${encodeURIComponent(name)}`),
  draftFile: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/skills/drafts/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`,
    ),
  create: (data: { name: string; description: string; content: string }) =>
    request<SkillDetail>('/skills', 'POST', data),
  update: (
    name: string,
    data: { enabled?: boolean; content?: string; description?: string; version?: string },
  ) => request<SkillDetail>(`/skills/${encodeURIComponent(name)}`, 'PATCH', data),
  remove: (name: string, version?: string) =>
    request(
      `/skills/${encodeURIComponent(name)}?version=${encodeURIComponent(version ?? '')}`,
      'DELETE',
    ),
  file: (name: string, path: string) =>
    request<SkillFile>(`/skills/${encodeURIComponent(name)}/file?path=${encodeURIComponent(path)}`),
  saveFile: (name: string, data: { path: string; content: string; version: string }) =>
    request<SkillFile>(`/skills/${encodeURIComponent(name)}/file`, 'PUT', data),
  prepare: (source: string, user_intent?: string) =>
    request<SkillDraft>('/skills/prepare', 'POST', { source, user_intent }),
  draft: (id: string) => request<SkillDraft>(`/skills/drafts/${encodeURIComponent(id)}`),
  confirm: (id: string, confirmation_token: string) =>
    request(`/skills/drafts/${encodeURIComponent(id)}/confirm`, 'POST', { confirmation_token }),
  cancel: (id: string) => request(`/skills/drafts/${encodeURIComponent(id)}/cancel`, 'POST', {}),
};

export const settingsApi = {
  configuration: () => request<ConfigurationSnapshot>('/configuration'),
  saveConfiguration: (revision: string, changes: Record<string, ConfigurationValue>) =>
    request<ConfigurationSnapshot>('/configuration', 'PATCH', { revision, changes }),
  memory: () => request<{ documents: MemoryDocument[] }>('/memory'),
  saveMemory: (target: string, content: string, revision: string) =>
    request<MemoryDocument>(`/memory/${encodeURIComponent(target)}`, 'PUT', { content, revision }),
  diagnostics: () => request<Diagnostics>('/diagnostics'),
};

export const schedulesApi = {
  list: (offset = 0) =>
    request<{
      items: ScheduledJob[];
      total: number;
      offset: number;
      limit: number;
      runtime: { running: boolean; owner?: string; reason?: string };
      defaults: ScheduleDefaults;
    }>(`/schedules?offset=${offset}&limit=50`),
  create: (data: ScheduleForm) => request<ScheduledJob>('/schedules', 'POST', data),
  update: (id: string, data: ScheduleForm) =>
    request<ScheduledJob>(`/schedules/${encodeURIComponent(id)}`, 'PATCH', data),
  remove: (id: string) => request(`/schedules/${encodeURIComponent(id)}`, 'DELETE'),
  enabled: (id: string, enabled: boolean) =>
    request<ScheduledJob>(`/schedules/${encodeURIComponent(id)}/enabled`, 'POST', { enabled }),
  run: (id: string) => request<ScheduleRun>(`/schedules/${encodeURIComponent(id)}/run`, 'POST', {}),
  preview: (schedule: ScheduleSpec) =>
    request<{ next_runs: number[]; schedule_label: string }>('/schedules/preview', 'POST', {
      schedule,
    }),
  runs: (id: string, offset = 0) =>
    request<{ items: ScheduleRun[] }>(
      `/schedules/${encodeURIComponent(id)}/runs?offset=${offset}&limit=30`,
    ),
  cancelRun: (id: string) =>
    request<ScheduleRun>(`/schedules/runs/${encodeURIComponent(id)}/cancel`, 'POST', {}),
  targets: () => request<{ items: ScheduleTarget[] }>('/schedule-targets'),
  updateTarget: (id: string, data: { display_name?: string; enabled?: boolean }) =>
    request<ScheduleTarget>(`/schedule-targets/${encodeURIComponent(id)}`, 'PATCH', data),
  removeTarget: (id: string) => request(`/schedule-targets/${encodeURIComponent(id)}`, 'DELETE'),
  testTarget: (id: string) =>
    request<{ success: boolean; status: string; error?: string }>(
      `/schedule-targets/${encodeURIComponent(id)}/test`,
      'POST',
      {},
    ),
  pair: (type: string, display_name?: string) =>
    request<SchedulePairing>('/schedule-targets/pairings', 'POST', { type, display_name }),
  pairing: (code: string) =>
    request<SchedulePairing>(`/schedule-targets/pairings/${encodeURIComponent(code)}`),
  cancelPairing: (code: string) =>
    request(`/schedule-targets/pairings/${encodeURIComponent(code)}`, 'DELETE'),
};

const filePath = (session: string, file = '') =>
  `/sessions/${encodeURIComponent(session)}/files${file ? `/${encodeURIComponent(file)}` : ''}`;
export const filesApi = {
  upload: (session: string, file: File) =>
    request<FileReference>(
      `${filePath(session)}?name=${encodeURIComponent(file.name)}`,
      'POST',
      file,
      { 'Content-Type': 'application/octet-stream' },
    ),
  link: (session: string, path: string) =>
    request<FileReference>(`${filePath(session)}/link`, 'POST', { path }),
  browse: (session: string, path?: string) =>
    request<FileBrowse>(
      `${filePath(session)}/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`,
    ),
  metadata: (session: string, file: string) => request<FileReference>(filePath(session, file)),
  preview: (session: string, file: string) =>
    request<FilePreview>(`${filePath(session, file)}/preview`),
  content: async (session: string, file: string): Promise<Blob> => {
    const headers: Record<string, string> = {
      'X-MClaw-Client': sessionStorage.getItem(CLIENT_KEY) ?? '',
    };
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
    let response: Response;
    try {
      response = await fetch(`${ROOT}${filePath(session, file)}/content`, {
        headers,
        credentials: 'same-origin',
      });
    } catch {
      throw new ApiError('disconnected', '无法连接本机服务，请稍后重试。', 0);
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new ApiError(
        payload.error?.code ?? 'file_failed',
        payload.error?.message ?? '无法读取文件',
        response.status,
      );
    }
    return response.blob();
  },
};

export const voiceApi = {
  status: () => request<VoiceStatus>('/voice'),
  configure: (data: { enabled: boolean; provider?: string; api_key?: string }) =>
    request<VoiceStatus>('/voice', 'PUT', data),
  transcribe: (audio: Blob, duration: number) =>
    request<{ text: string }>('/voice/transcribe', 'POST', audio, {
      'Content-Type': audio.type,
      'X-Audio-Duration-Ms': String(duration),
    }),
};

export function subscribeSession(
  id: string,
  onEvent: (event: SessionEvent) => void,
  onConnection: (connected: boolean) => void,
) {
  let disposed = false;
  let socket: WebSocket | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let delay = 1000;
  let lastSeq = -1;
  let snapshot: Session | undefined;
  const connect = () => {
    if (disposed) return;
    const url = new URL(`${ROOT}/sessions/${encodeURIComponent(id)}/events`, window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('token', sessionStorage.getItem(TOKEN_KEY) ?? '');
    url.searchParams.set('client', sessionStorage.getItem(CLIENT_KEY) ?? '');
    url.searchParams.set('incremental', '1');
    socket = new WebSocket(url);
    socket.onopen = () => {
      delay = 1000;
      onConnection(true);
    };
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as SessionEvent & {
          base_seq?: number;
          changes?: SnapshotChange[];
        };
        // Each reconnect begins from an authoritative snapshot; sequence may reset on restart.
        if (data.type === 'snapshot') {
          lastSeq = data.seq ?? -1;
          snapshot = data.session;
        } else if (data.seq !== undefined && data.seq <= lastSeq) return;
        else if (data.type === 'patch') {
          if (!snapshot || data.base_seq !== lastSeq || !Array.isArray(data.changes)) {
            socket?.close();
            return;
          }
          snapshot = applySnapshotChanges(snapshot, data.changes);
          lastSeq = data.seq ?? lastSeq;
          onEvent({ type: 'snapshot', seq: lastSeq, session: snapshot });
          return;
        } else lastSeq = data.seq ?? lastSeq;
        onEvent(data);
      } catch {
        // Reconnect from the authoritative snapshot if a patch could not be applied.
        socket?.close();
      }
    };
    socket.onerror = () => socket?.close();
    socket.onclose = (event) => {
      if (disposed) return;
      if (event.code === 4409) {
        onEvent({ type: 'occupied' });
        return;
      }
      onConnection(false);
      retry = setTimeout(connect, delay);
      delay = Math.min(delay * 2, 15000);
    };
  };
  connect();
  return () => {
    disposed = true;
    if (retry) clearTimeout(retry);
    socket?.close();
  };
}
