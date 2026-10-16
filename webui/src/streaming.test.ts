import { expect, it } from 'vitest';
import { applySnapshotChanges } from './streaming';
import type { Session } from './types';

it('updates streamed replies without replacing completed messages or tool details', () => {
  const session: Session = {
    id: 's',
    title: '任务',
    status: 'running',
    queue: [],
    messages: [
      { id: 'old', role: 'assistant', content: '历史回复' },
      {
        id: 'live',
        role: 'assistant',
        content: '正在',
        process: [{ id: 'tool', kind: 'tool', title: 'terminal', output: '已完成' }],
      },
    ],
  };
  const next = applySnapshotChanges(session, [
    { op: 'append', path: ['messages', 1, 'content'], value: '检查' },
  ]);
  expect(next.messages[1].content).toBe('正在检查');
  expect(session.messages[1].content).toBe('正在');
  expect(next.messages[0]).toBe(session.messages[0]);
  expect(next.messages[1].process).toBe(session.messages[1].process);
});

it('preserves unrelated children while updating a child, resetting text, and appending turns', () => {
  const session: Session = {
    id: 's',
    title: '任务',
    status: 'running',
    queue: [],
    error: 'old',
    messages: [
      {
        id: 'main',
        role: 'assistant',
        content: '主回复',
        delegations: {
          group: {
            tasks: {
              '0': {
                task_index: 0,
                goal: '任务',
                status: 'running',
                messages: [{ id: 'child', role: 'assistant', content: '进展' }],
              },
              '1': { task_index: 1, goal: '另一个任务', status: 'completed', messages: [] },
            },
          },
        },
      },
    ],
  };
  const next = applySnapshotChanges(session, [
    {
      op: 'set',
      path: ['messages', 0, 'delegations', 'group', 'tasks', '0', 'messages', 0, 'content'],
      value: '',
    },
    { op: 'remove', path: ['error'] },
    { op: 'append', path: ['messages'], value: [{ id: 'next', role: 'user', content: '继续' }] },
  ]);
  expect(next.messages[0].delegations!.group.tasks!['0'].messages[0].content).toBe('');
  expect(next.messages[0].delegations!.group.tasks!['1']).toBe(
    session.messages[0].delegations!.group.tasks!['1'],
  );
  expect(next.messages).toHaveLength(2);
  expect(next.error).toBeUndefined();
  expect(session.error).toBe('old');
  expect(() =>
    applySnapshotChanges(session, [{ op: 'set', path: ['__proto__', 'bad'], value: true }]),
  ).toThrow();
});
