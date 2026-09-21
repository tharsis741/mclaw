import { describe, expect, it } from 'vitest';
import { groupSessions, primaryAction } from './session-state';
import type { Session } from './types';

const session = (status: Session['status'], extra: Partial<Session> = {}): Session => ({
  id: 's1',
  title: '测试任务',
  status,
  messages: [],
  queue: [],
  ...extra,
});
describe('server-driven composer actions', () => {
  it('queues running input and disables a stopping task action', () => {
    expect(primaryAction(session('running'), '新任务')).toBe('排队发送');
    expect(primaryAction(session('waiting'), '新任务')).toBe('排队发送');
    expect(primaryAction(session('stopping'), '补充')).toBe('正在停止');
  });
  it('resumes the original interrupted task before queued work', () => {
    expect(primaryAction(session('interrupted', { active_task: { id: 'original' } }), '')).toBe(
      '继续处理',
    );
    expect(primaryAction(session('failed', { active_task: { id: 'original' } }), '修复说明')).toBe(
      '补充并继续',
    );
    expect(primaryAction(session('interrupted'), '保留草稿')).toBe('继续处理');
  });
  it('groups only explicitly selected workspaces while preserving supplied order', () => {
    const result = groupSessions(
      [
        session('running', { id: 'a', title: '默认目录', workspace: '/results/a' }),
        session('idle', {
          id: 'b',
          title: '指定目录',
          workspace: '/project',
          workspace_explicit: true,
        }),
        session('completed', { id: 'c', title: '较早会话' }),
      ],
      '',
    );
    expect(result.map((group) => group.title)).toEqual(['最近会话', '/project']);
    expect(result[0].sessions.map((item) => item.id)).toEqual(['a', 'c']);
    expect(
      groupSessions(
        result.flatMap((group) => group.sessions),
        '指定',
      )[0].sessions[0].id,
    ).toBe('b');
  });
});
