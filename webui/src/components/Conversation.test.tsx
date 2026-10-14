import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { Conversation } from './Conversation';
import type { FileReference, Session } from '../types';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('shows stable local timestamps below user and assistant messages, including imported history', () => {
  const moment = new Date('2026-09-23T08:06:15Z');
  const session: Session = {
    id: 'times',
    title: '消息时间',
    status: 'running',
    queue: [],
    messages: [
      { id: 'u', role: 'user', content: '你好', created_at: moment.getTime() / 1000 },
      { id: 'a', role: 'assistant', content: '正在回复', created_at: moment.toISOString() },
      { id: 'old', role: 'assistant', content: '未记录时间的旧消息' },
      { id: 'bad', role: 'user', content: '损坏的时间', created_at: 'invalid' },
    ],
  };
  const view = render(<Conversation session={session} />);
  const times = view.container.querySelectorAll('time');
  expect(times).toHaveLength(2);
  for (const time of times) {
    expect(time).toHaveAttribute('datetime', moment.toISOString());
    expect(time).toHaveAttribute('title', expect.stringContaining('2026'));
    expect(time).toBeVisible();
    const body = time.closest('article')!.querySelector('.markdown')!;
    expect(body.compareDocumentPosition(time) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(time.closest('.message-identity')).toBeNull();
  }
  expect(times[0].textContent).toBe(times[1].textContent);
  view.rerender(
    <Conversation
      session={{
        ...session,
        messages: session.messages.map((message) =>
          message.id === 'a' ? { ...message, content: '回复完成' } : message,
        ),
      }}
    />,
  );
  expect(view.container.querySelectorAll('time')[1]).toHaveAttribute(
    'datetime',
    moment.toISOString(),
  );
});

it('links remote progress to its exact tool call, preserving a separate failed call and the real call count', async () => {
  const file: FileReference = {
    id: 'remote-file',
    name: '分析.md',
    path: '/session/分析.md',
    source: 'remote',
    size: 10,
    modified_at: 1,
    created_at: 1,
    device: '电脑 A',
    workspace: '',
    mime_type: 'text/markdown',
  };
  const preview = vi.fn();
  const session: Session = {
    id: 's',
    title: '任务',
    status: 'completed',
    queue: [],
    messages: [
      {
        id: 'm',
        role: 'assistant',
        content: '最终答复',
        attachments: [file],
        process: [
          {
            id: 'tool-one',
            kind: 'tool',
            tool_call_id: 'one',
            title: 'dsoftbus_run_agent_task',
            status: 'completed',
          },
          {
            id: 'remote-one',
            kind: 'remote',
            tool_call_id: 'one',
            title: '远程任务',
            device_name: '电脑 A',
            task_id: 'task-one',
            status: 'waiting',
            progress: [
              { id: 1, content: '读取第一份资料' },
              { id: 2, content: '生成分析文件' },
            ],
            input_request: { message: '请补充另一份报表', accepts: ['file'] },
            output: 'null',
          },
          {
            id: 'tool-two',
            kind: 'tool',
            tool_call_id: 'two',
            title: 'dsoftbus_run_agent_task',
            status: 'failed',
            output: '{"error":"文件路径无效"}',
          },
        ],
      },
    ],
  };
  const view = render(<Conversation session={session} onFilePreview={preview} />);
  expect(screen.getByText('2 次工具调用')).toBeVisible();
  expect(screen.getByText('最终答复')).toBeVisible();
  expect(screen.getByText('读取第一份资料')).not.toBeVisible();
  expect(view.container.querySelectorAll('.execution-entry')).toHaveLength(2);
  await userEvent.click(screen.getByText('电脑 A'));
  expect(screen.getByText('读取第一份资料')).toBeVisible();
  expect(screen.getByText('请补充另一份报表')).toBeVisible();
  expect(screen.getByText('生成分析文件')).toBeVisible();
  expect(screen.getByText('来自 电脑 A')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: /分析.md/ }));
  expect(preview).toHaveBeenCalledWith(file);
});

it('renders persisted skill source objects in message details', async () => {
  render(
    <Conversation
      session={{
        id: 's',
        title: '任务',
        status: 'completed',
        queue: [],
        messages: [
          {
            id: 'm',
            role: 'user',
            content: '执行周报',
            skills: [
              {
                name: 'weekly',
                source: { type: 'local', original: 'C:/skills/weekly' },
                version: 'snapshot-v1',
              },
            ],
          },
        ],
      }}
    />,
  );
  await userEvent.click(screen.getByText('weekly'));
  expect(screen.getByText('C:/skills/weekly')).toBeVisible();
  expect(screen.getByText('snapshot-v1')).toBeVisible();
});

it('updates a supplement from received to applied using the persisted server state', () => {
  const session: Session = {
    id: 'supplement-session',
    title: '任务',
    status: 'running',
    queue: [],
    messages: [
      {
        id: 'supplement',
        role: 'user',
        content: '补充原任务的要求',
        mode: 'steer',
        applied: false,
      },
    ],
  };
  const { rerender, unmount } = render(<Conversation session={session} />);
  expect(screen.getByText('补充当前任务 · 已接收')).toBeVisible();
  const applied: Session = {
    ...session,
    status: 'completed',
    messages: [{ ...session.messages[0], applied: true }],
  };
  rerender(<Conversation session={applied} />);
  expect(screen.getByText('补充当前任务 · 已应用')).toBeVisible();
  expect(screen.queryByText('补充当前任务 · 已接收')).not.toBeInTheDocument();
  unmount();
  render(<Conversation session={applied} />);
  expect(screen.getByText('补充当前任务 · 已应用')).toBeVisible();
  expect(screen.getByText('补充原任务的要求')).toBeVisible();
});

it('shows individual tools, counts calls only, and retains expansion across streamed snapshots', async () => {
  const session: Session = {
    id: 's',
    title: 'task',
    status: 'running',
    queue: [],
    messages: [
      {
        id: 'm',
        role: 'assistant',
        content: '正在生成正文',
        process: [
          {
            id: 'r',
            kind: 'reasoning',
            title: '思考',
            content: '先看目录结构',
            status: 'completed',
          },
          { id: 'n', kind: 'progress', title: '执行进展', content: '正在读取说明文件。' },
          {
            id: 'p',
            kind: 'tool',
            title: 'terminal',
            arguments: '{"command":"cat README.md"}',
            output: '{"output":"项目说明","exit_code":0}',
            status: 'completed',
          },
        ],
      },
    ],
  };
  const { rerender } = render(<Conversation session={session} />);
  expect(screen.getByText('1 次工具调用')).toBeVisible();
  expect(screen.getByText('正在读取说明文件。')).toBeVisible();
  const details = screen.getByText('运行命令').closest('details')!;
  expect(details.open).toBe(false);
  await userEvent.click(screen.getByText('运行命令'));
  expect(details.open).toBe(true);
  expect(screen.getByText('输出')).toBeVisible();
  expect(screen.getByText(/项目说明/)).toBeVisible();
  expect(screen.getByText(/"exit_code": 0/)).toBeVisible();
  expect(screen.queryByText('参数')).not.toBeInTheDocument();
  rerender(
    <Conversation
      session={{
        ...session,
        messages: [{ ...session.messages[0], content: '正在生成正文，新的内容。' }],
      }}
    />,
  );
  expect(screen.getByText('运行命令').closest('details')!.open).toBe(true);
  expect(screen.getByText('正在生成正文，新的内容。')).toBeVisible();
});

it('folds live reasoning by default and preserves manual expansion through streaming and completion', async () => {
  const session: Session = {
    id: 'reasoning',
    title: '任务',
    status: 'running',
    queue: [],
    messages: [
      {
        id: 'm',
        role: 'assistant',
        content: '',
        process: [
          { id: 'r', kind: 'reasoning', title: '思考', status: 'running', content: '先确认文件' },
        ],
      },
    ],
  };
  const { rerender } = render(<Conversation session={session} />);
  expect(screen.queryByText('先确认文件')).not.toBeInTheDocument();
  expect(screen.getByText('思考中')).toBeVisible();
  await userEvent.click(screen.getByText('思考中'));
  expect(screen.getByText('先确认文件')).toBeVisible();
  rerender(
    <Conversation
      session={{
        ...session,
        messages: [
          {
            ...session.messages[0],
            process: [
              {
                id: 'r',
                kind: 'reasoning',
                title: '思考',
                status: 'running',
                content: '先确认文件的位置。',
              },
            ],
          },
        ],
      }}
    />,
  );
  expect(screen.getByText('先确认文件的位置。')).toBeVisible();
  const completed: Session = {
    ...session,
    status: 'completed',
    messages: [
      {
        ...session.messages[0],
        content: '文件已找到。',
        process: [
          {
            id: 'r',
            kind: 'reasoning',
            title: '思考',
            status: 'completed',
            content: '已确认位置。',
          },
        ],
      },
    ],
  };
  rerender(<Conversation session={completed} />);
  expect(screen.getByText('思考完成')).toBeVisible();
  expect(screen.getByText('已确认位置。')).toBeVisible();
  await userEvent.click(screen.getByText('思考完成'));
  rerender(<Conversation session={{ ...completed, usage: { total_tokens: 100 } }} />);
  expect(screen.queryByText('已确认位置。')).not.toBeInTheDocument();
  expect(screen.getByText('文件已找到。')).toBeVisible();
});

function progressSession(content: string): Session {
  return {
    id: 'progress-session',
    title: '任务',
    status: 'running',
    queue: [],
    messages: [
      {
        id: 'reply',
        role: 'assistant',
        content: '',
        process: [{ id: 'note', kind: 'progress', title: '执行进展', content }],
      },
    ],
  };
}

it('adds expansion only when progress overflows and remeasures streamed text and viewport changes', async () => {
  let scrollHeight = 21;
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(42);
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => scrollHeight);
  let resized!: () => void;
  const disconnect = vi.fn();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: () => void) {
        resized = callback;
      }
      observe() {}
      disconnect = disconnect;
    },
  );
  const { rerender, unmount } = render(<Conversation session={progressSession('先检查配置。')} />);
  expect(screen.getByText('先检查配置。')).toBeVisible();
  expect(screen.queryByRole('button', { name: '展开任务进展' })).not.toBeInTheDocument();
  scrollHeight = 84;
  rerender(
    <Conversation session={progressSession('先检查配置，再比对调用记录，最后确认修复结果。')} />,
  );
  const expand = screen.getByRole('button', { name: '展开任务进展' });
  expect(expand).toHaveAttribute('aria-expanded', 'false');
  const content = document.getElementById(expand.getAttribute('aria-controls')!)!;
  expect(content).toHaveClass('is-collapsed');
  await userEvent.click(expand);
  expect(content).not.toHaveClass('is-collapsed');
  rerender(<Conversation session={progressSession('已找到差异，正在进一步确认。')} />);
  expect(screen.getByRole('button', { name: '收起任务进展' })).toHaveAttribute(
    'aria-expanded',
    'true',
  );
  expect(screen.getByText('已找到差异，正在进一步确认。')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '收起任务进展' }));
  scrollHeight = 21;
  act(() => resized());
  expect(screen.queryByRole('button', { name: '展开任务进展' })).not.toBeInTheDocument();
  scrollHeight = 84;
  act(() => resized());
  expect(screen.getByRole('button', { name: '展开任务进展' })).toBeVisible();
  unmount();
  expect(disconnect).toHaveBeenCalled();
});

it('remeasures progress on window resize when ResizeObserver is unavailable', () => {
  vi.stubGlobal('ResizeObserver', undefined);
  let scrollHeight = 21;
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(42);
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => scrollHeight);
  render(<Conversation session={progressSession('窄屏时可能需要展开的进展。')} />);
  expect(screen.queryByRole('button', { name: '展开任务进展' })).not.toBeInTheDocument();
  scrollHeight = 84;
  fireEvent(window, new Event('resize'));
  expect(screen.getByRole('button', { name: '展开任务进展' })).toBeVisible();
});

it('keeps a manually folded process closed as new entries arrive and the final answer completes', async () => {
  const user = userEvent.setup();
  const session = progressSession('开始检查文件。');
  session.messages[0].process!.push({
    id: 'tool',
    kind: 'tool',
    title: 'read_file',
    status: 'completed',
    arguments: '{"path":"README.md"}',
    output: '文件内容',
  });
  const { rerender } = render(<Conversation session={session} />);
  await user.click(screen.getByText('读取文件'));
  expect(screen.getByText('文件内容')).toBeVisible();
  const toggle = screen.getByRole('button', { name: '执行过程 1 次工具调用' });
  toggle.focus();
  await user.keyboard('{Enter}');
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(screen.getByText('开始检查文件。')).not.toBeVisible();
  const completed: Session = {
    ...session,
    status: 'completed',
    messages: [
      {
        ...session.messages[0],
        content: '已经完成检查。',
        process: [
          ...session.messages[0].process!,
          {
            id: 'another-tool',
            kind: 'tool',
            title: 'read_file',
            status: 'completed',
            arguments: '{"path":"pyproject.toml"}',
            output: '更多内容',
          },
        ],
      },
    ],
  };
  rerender(<Conversation session={completed} />);
  expect(screen.getByRole('button', { name: '执行过程 2 次工具调用' })).toHaveAttribute(
    'aria-expanded',
    'false',
  );
  expect(screen.queryByText('更多内容')).not.toBeInTheDocument();
  expect(screen.getByText('已经完成检查。')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '复制回复' }));
  expect(await navigator.clipboard.readText()).toBe('已经完成检查。');
  await user.click(screen.getByRole('button', { name: '执行过程 2 次工具调用' }));
  expect(screen.getByText('文件内容')).toBeVisible();
  expect(screen.queryByText('更多内容')).not.toBeInTheDocument();
});

it('does not carry a folded process into a different session with the same message ID', async () => {
  const session = progressSession('检查当前会话。');
  const { rerender } = render(<Conversation session={session} />);
  await userEvent.click(screen.getByRole('button', { name: '执行过程' }));
  rerender(<Conversation session={{ ...session, id: 'another-session' }} />);
  expect(screen.getByRole('button', { name: '执行过程' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText('检查当前会话。')).toBeVisible();
});

it.each([
  ['paused', '队列已暂停，可以在输入区继续处理。'],
  ['interrupted', '已停止 · 进展已保存，可以在输入区继续处理。'],
  ['stopping', '正在停止，等待工具收尾'],
] as const)('preserves the existing %s hint outside a collapsed process', async (status, hint) => {
  render(<Conversation session={{ ...progressSession('检查进度。'), status }} />);
  await userEvent.click(screen.getByRole('button', { name: '执行过程' }));
  expect(screen.getByRole('status')).toHaveTextContent(hint);
  expect(screen.getByRole('status')).toBeVisible();
});

it('labels a failed tool and does not count reasoning as a tool call', async () => {
  render(
    <Conversation
      session={{
        id: 's',
        title: '任务',
        status: 'completed',
        queue: [],
        messages: [
          {
            id: 'm',
            role: 'assistant',
            content: '下载未完成',
            process: [
              { id: 'r', kind: 'reasoning', title: '思考', content: '尝试下载' },
              {
                id: 't',
                kind: 'tool',
                title: 'browser_download',
                status: 'failed',
                arguments: '{"url":"https://example.test/paper.pdf"}',
                output: '{"output":"","error":"timeout"}',
              },
            ],
          },
        ],
      }}
    />,
  );
  expect(screen.getByText('1 次工具调用')).toBeVisible();
  expect(screen.getByText('失败')).toBeVisible();
  await userEvent.click(screen.getByText('下载文件'));
  expect(screen.getByText(/"error": "timeout"/)).toBeVisible();
});

it('shows a confirmation request separately from failure and missing information', () => {
  const view = render(
    <Conversation
      session={{
        id: 'confirmation',
        title: '安装技能',
        status: 'completed',
        queue: [],
        messages: [
          {
            id: 'response',
            role: 'assistant',
            content: '技能已准备好，请确认是否启用。',
            process: [
              {
                id: 'tool-prepare',
                kind: 'tool',
                title: 'skill_manage',
                status: 'awaiting_confirmation',
                output: JSON.stringify({ success: false, requires_confirmation: true }),
              },
            ],
          },
        ],
      }}
    />,
  );
  expect(screen.getByText('等待确认')).toBeVisible();
  expect(screen.queryByText('失败')).not.toBeInTheDocument();
  expect(screen.queryByText('等待补充')).not.toBeInTheDocument();
  expect(view.container.querySelector('.entry-status.awaiting_confirmation')).toHaveTextContent(
    '等待确认',
  );
});
