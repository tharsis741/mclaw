import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { Conversation } from './Conversation';
import type { Session } from '../types';

function example(): Session {
  return {
    id: 's',
    title: '任务',
    status: 'running',
    queue: [],
    messages: [
      {
        id: 'reply',
        role: 'assistant',
        content: '主代理的回复',
        process: [
          {
            id: 'delegate',
            kind: 'tool',
            title: 'delegate_task',
            tool_call_id: 'call',
            status: 'running',
          },
        ],
        delegations: {
          batch: {
            tool_call_id: 'call',
            tasks: {
              '0': {
                task_index: 0,
                goal: '检查接口逻辑',
                status: 'running',
                model: 'test-model',
                messages: [
                  {
                    id: 'assignment',
                    role: 'user',
                    source: 'delegate',
                    content: '检查接口逻辑，核对错误处理。',
                  },
                  {
                    id: 'child-reply',
                    role: 'assistant',
                    content: '正在核对返回结果。',
                    process: [
                      {
                        id: 'reason',
                        kind: 'reasoning',
                        title: '思考',
                        status: 'completed',
                        content: '先检查接口定义。',
                      },
                      {
                        id: 'tool',
                        kind: 'tool',
                        title: 'terminal',
                        tool_call_id: 'child-call',
                        arguments: '{"command":"python -m pytest tests/test_api.py"}',
                        output: '{"stdout":"12 passed","exit_code":0}',
                        status: 'completed',
                      },
                    ],
                  },
                ],
              },
              '1': {
                task_index: 1,
                goal: '检查文档',
                status: 'completed',
                messages: [{ id: 'other', role: 'assistant', content: '文档已检查。' }],
              },
            },
          },
        },
      },
    ],
  };
}

it('opens the ordinary conversation design and keeps child tools out of the main reply', async () => {
  const session = example();
  const { rerender } = render(<Conversation session={session} />);
  expect(screen.getByText('1 次工具调用')).toBeVisible();
  expect(screen.queryByText('运行命令')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '查看子代理：检查接口逻辑' }));
  const dialog = within(screen.getByRole('dialog'));
  expect(dialog.getByText('主代理')).toBeVisible();
  expect(dialog.getByText('检查接口逻辑，核对错误处理。')).toBeVisible();
  expect(dialog.getByText('正在核对返回结果。')).toBeVisible();
  expect(dialog.queryByText('先检查接口定义。')).not.toBeInTheDocument();
  await userEvent.click(dialog.getByText('运行命令'));
  expect(dialog.getByText('命令')).toBeVisible();
  expect(dialog.getByText(/12 passed/)).toBeVisible();
  const next = structuredClone(session);
  const task = next.messages[0].delegations!.batch.tasks!['0'];
  task.status = 'completed';
  task.duration_seconds = 9;
  task.messages[1].content = '接口测试通过。';
  rerender(<Conversation session={next} />);
  expect(dialog.getByText('接口测试通过。')).toBeVisible();
  expect(dialog.getByText('运行命令').closest('details')!.open).toBe(true);
  expect(dialog.getByText('9 秒')).toBeVisible();
});

it('closes by Escape, backdrop and button, restores focus and preserves expanded tools on reopen', async () => {
  render(<Conversation session={example()} />);
  const trigger = screen.getByRole('button', { name: '查看子代理：检查接口逻辑' });
  await userEvent.click(trigger);
  await userEvent.click(within(screen.getByRole('dialog')).getByText('运行命令'));
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  await userEvent.click(trigger);
  expect(within(screen.getByRole('dialog')).getByText(/12 passed/)).toBeVisible();
  fireEvent.click(screen.getByRole('dialog').parentElement!);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole('button', { name: '关闭子代理详情' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByText('主代理的回复')).toBeVisible();
});

it('shows the saved legacy result without inventing tool details', async () => {
  const session = example();
  session.messages[0].delegations = {
    old: {
      goals: ['旧任务'],
      result: { results: [{ task_index: 0, status: 'timed_out', summary: '已找到部分资料。' }] },
    },
  };
  render(<Conversation session={session} />);
  await userEvent.click(screen.getByRole('button', { name: '查看子代理：旧任务' }));
  const dialog = within(screen.getByRole('dialog'));
  expect(dialog.getByText('已超时')).toBeVisible();
  expect(dialog.getByText('已找到部分资料。')).toBeVisible();
  expect(dialog.getByText('这条历史记录只保存了任务状态和结果。')).toBeVisible();
  expect(dialog.queryByText('执行过程')).not.toBeInTheDocument();
});

it('keeps scroll position when reading older child content during live updates', async () => {
  const session = example();
  const scroll = vi.spyOn(Element.prototype, 'scrollIntoView');
  const { rerender } = render(<Conversation session={session} />);
  await userEvent.click(screen.getByRole('button', { name: '查看子代理：检查接口逻辑' }));
  const scroller = screen.getByRole('dialog').querySelector('.conversation')!;
  Object.defineProperties(scroller, {
    scrollHeight: { configurable: true, value: 2200 },
    clientHeight: { configurable: true, value: 400 },
  });
  scroller.scrollTop = 300;
  fireEvent.scroll(scroller);
  scroll.mockClear();
  const next = structuredClone(session);
  next.messages[0].delegations!.batch.tasks!['0'].messages[1].content += ' 新进展。';
  rerender(<Conversation session={next} />);
  // The main conversation may follow its own stream, but the child stays where the reader is.
  expect(scroller.scrollTop).toBe(300);
  expect(
    within(screen.getByRole('dialog')).getByRole('button', { name: '回到最新' }),
  ).toBeVisible();
  scroll.mockRestore();
});
