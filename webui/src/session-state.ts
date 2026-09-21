import type { Session, SessionStatus } from './types';

export const STATUS_TEXT: Record<SessionStatus, string> = {
  idle: '准备就绪',
  running: '正在处理',
  waiting: '等待处理',
  stopping: '正在停止，等待工具收尾',
  stopped: '已停止 · 进展已保存',
  interrupted: '已停止 · 进展已保存',
  paused: '队列已暂停',
  completed: '已完成',
  failed: '需要处理',
};
export const isRunning = (session?: Session | null) =>
  session?.status === 'running' || session?.status === 'waiting';
export const isPaused = (session?: Session | null) =>
  !!session && ['stopped', 'interrupted', 'paused', 'failed'].includes(session.status);

export function primaryAction(session: Session | null, content: string): string {
  if (session?.status === 'stopping') return '正在停止';
  if (isPaused(session)) return content.trim() && session?.active_task ? '补充并继续' : '继续处理';
  if (isRunning(session)) return '排队发送';
  return '发送';
}

export function groupSessions(
  sessions: Session[],
  search: string,
): { title: string; sessions: Session[] }[] {
  const filtered = sessions.filter((s) =>
    `${s.title} ${s.workspace ?? ''}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
  );
  const groups = new Map<string, Session[]>();
  for (const session of filtered) {
    const title = session.workspace_explicit && session.workspace ? session.workspace : '最近会话';
    if (!groups.has(title)) groups.set(title, []);
    groups.get(title)!.push(session);
  }
  return [...groups].map(([title, items]) => ({ title, sessions: items }));
}

const DRAFT_KEY = 'mclaw.drafts.v1';
const PENDING_KEY = 'mclaw.pending-submissions.v1';
const SKILLS_KEY = 'mclaw.draft-skills.v1';

export function readDraftSkills(key = SKILLS_KEY): Record<string, string[]> {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? '{}');
    return Object.fromEntries(
      Object.entries(value).filter(
        (entry): entry is [string, string[]] =>
          Array.isArray(entry[1]) && entry[1].every((item) => typeof item === 'string'),
      ),
    );
  } catch {
    return {};
  }
}
export function saveDraftSkills(value: Record<string, string[]>, key = SKILLS_KEY) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* The in-memory selection remains available. */
  }
}

export function readPendingSubmissions(): Record<string, string> {
  try {
    const value = JSON.parse(localStorage.getItem(PENDING_KEY) ?? '{}');
    return Object.fromEntries(
      Object.entries(value).filter(
        (entry): entry is [string, string] => typeof entry[1] === 'string',
      ),
    );
  } catch {
    return {};
  }
}

export function savePendingSubmissions(pending: Record<string, string>) {
  try {
    localStorage.setItem(PENDING_KEY, JSON.stringify(pending));
  } catch {
    /* In-memory IDs still protect retry while the page remains open. */
  }
}
export function readDrafts(): Record<string, string> {
  try {
    const drafts = JSON.parse(localStorage.getItem(DRAFT_KEY) ?? '{}');
    return drafts && typeof drafts === 'object' ? drafts : {};
  } catch {
    return {};
  }
}
export function saveDrafts(drafts: Record<string, string>) {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(drafts));
  } catch {
    /* The current in-memory draft remains available if storage is full. */
  }
}
