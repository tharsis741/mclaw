import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import App from './App';
import { api, devicesApi, providersApi, skillsApi, subscribeSession } from './api';
import type { Session, SessionEvent } from './types';
import { WELCOME_ACTIONS } from './welcome-content';

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api');
  return {
    ...actual,
    settingsApi: {
      ...actual.settingsApi,
      configuration: vi.fn().mockResolvedValue({ revision: 'r1', groups: [] }),
    },
    initializeConnection: vi.fn().mockResolvedValue(undefined),
    subscribeSession: vi.fn(() => () => {}),
    skillsApi: { ...actual.skillsApi, list: vi.fn() },
    devicesApi: { ...actual.devicesApi, status: vi.fn() },
    providersApi: {
      ...actual.providersApi,
      list: vi.fn(),
      refreshLibrary: vi.fn(),
      library: vi.fn(),
    },
    api: {
      bootstrap: vi.fn(),
      sessions: vi.fn(),
      session: vi.fn(),
      createSession: vi.fn(),
      submit: vi.fn(),
      updateSession: vi.fn(),
      deleteSession: vi.fn(),
      exportSession: vi.fn(),
      resume: vi.fn(),
      stop: vi.fn(),
      setupStart: vi.fn(),
      setupStatus: vi.fn(),
    },
  };
});

it('restores the session device choice, submits it with the task, and retains it after sending', async () => {
  const id = 'urn:mclaw:device:oh:' + 'a'.repeat(64);
  vi.mocked(api.bootstrap).mockResolvedValue({
    setup_required: false,
    version: 'test',
    device: { name: '本机', platform: 'kaihong' },
    models: [],
    capabilities: [],
  });
  vi.mocked(devicesApi.status).mockResolvedValue({
    supported: true,
    enabled: true,
    state: 'ready',
    provider_ready: true,
    error: null,
    operation: null,
    localDevice: null,
    discoveryWarning: null,
    pairableDevices: [],
    trustedDevices: [
      {
        publicDeviceId: id,
        deviceIdSha256: 'a'.repeat(64),
        deviceName: '电脑 A',
        deviceTypeId: 12,
        online: true,
      },
    ],
    devices: [
      {
        deviceId: id,
        deviceName: '电脑 A',
        agentAvailability: 'READY',
        connectionState: 'CONNECTED',
        devicePresence: 'ONLINE',
      },
    ],
  });
  const session = { ...base, draft_devices: [id] };
  vi.mocked(api.session).mockResolvedValue(session);
  vi.mocked(api.submit).mockResolvedValue(session);
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: '准备就绪 原会话' }));
  expect(await screen.findByRole('button', { name: '设备 1' })).toBeEnabled();
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '请整理报表');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() =>
    expect(api.submit).toHaveBeenCalledWith(
      's1',
      '请整理报表',
      'queue',
      expect.any(String),
      [],
      [],
      [id],
    ),
  );
  expect(screen.getByRole('button', { name: '设备 1' })).toBeEnabled();
  await waitFor(() =>
    expect(api.updateSession).toHaveBeenCalledWith(
      's1',
      expect.objectContaining({ draft_devices: [id] }),
    ),
  );
});
const base: Session = {
  id: 's1',
  title: '原会话',
  status: 'idle',
  messages: [{ id: 'm1', role: 'user', content: '原历史' }],
  queue: [],
  model: 'test-model',
};
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(api.bootstrap).mockResolvedValue({
    setup_required: false,
    version: 'test',
    device: { name: '测试设备', platform: 'windows' },
    models: [{ id: 'test-model', label: '测试模型' }],
    default_model: 'test-model',
    capabilities: [],
  });
  vi.mocked(api.sessions).mockResolvedValue([base]);
  vi.mocked(api.session).mockResolvedValue(base);
  vi.mocked(api.updateSession).mockResolvedValue(base);
  vi.mocked(api.deleteSession).mockResolvedValue({ deleted: true, id: base.id });
  vi.mocked(skillsApi.list).mockResolvedValue({ skills: [] });
  vi.mocked(providersApi.list).mockResolvedValue({ catalog: [], configured: [], revision: 'test' });
  vi.mocked(subscribeSession).mockImplementation(() => () => {});
});

it('dismisses session actions on outside click, Escape, repeated click and session navigation', async () => {
  render(<App />);
  const trigger = await screen.findByRole('button', { name: '原会话 的菜单' });
  await userEvent.click(trigger);
  expect(screen.getByRole('menu', { name: '会话操作' })).toBeVisible();
  await userEvent.click(screen.getByRole('heading', { name: '今天，一起完成什么？' }));
  expect(screen.queryByRole('menu')).toBeNull();
  await userEvent.click(trigger);
  await userEvent.keyboard('{End}');
  expect(screen.getByRole('menuitem', { name: '删除会话' })).toHaveFocus();
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('menu')).toBeNull();
  expect(trigger).toHaveFocus();
  await userEvent.click(trigger);
  await userEvent.click(trigger);
  expect(screen.queryByRole('menu')).toBeNull();
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  expect(screen.queryByRole('menu')).toBeNull();
  expect(await screen.findByText('原历史')).toBeVisible();
});

it('keeps rename clickable inside the dismissible menu', async () => {
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: '原会话 的菜单' }));
  await userEvent.click(screen.getByRole('menuitem', { name: '重命名' }));
  expect(screen.queryByRole('menu')).toBeNull();
  const input = screen.getByRole('textbox', { name: '会话名称' });
  expect(input).toHaveFocus();
  await userEvent.clear(input);
  await userEvent.type(input, '新的名称{Enter}');
  await waitFor(() =>
    expect(api.updateSession).toHaveBeenCalledWith(base.id, { title: '新的名称' }),
  );
});

it('closes export actions immediately while retaining a subsequently opened menu', async () => {
  let reject!: (error: Error) => void;
  vi.mocked(api.exportSession).mockImplementationOnce(
    () =>
      new Promise((_, fail) => {
        reject = fail;
      }),
  );
  render(<App />);
  const trigger = await screen.findByRole('button', { name: '原会话 的菜单' });
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole('menuitem', { name: '导出 JSON' }));
  expect(api.exportSession).toHaveBeenCalledExactlyOnceWith('s1');
  expect(screen.queryByRole('menu')).toBeNull();
  await userEvent.click(trigger);
  await act(async () => reject(new Error('测试导出失败')));
  expect(screen.getByRole('menu')).toBeVisible();
});

it('refreshes deletions made while a different window owned the session', async () => {
  vi.useFakeTimers();
  try {
    render(<App />);
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(screen.getByRole('button', { name: '原会话 的菜单' })).toBeInTheDocument();
    vi.mocked(api.sessions).mockResolvedValue([]);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.queryByRole('button', { name: '原会话 的菜单' })).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

it('cancels session deletion using the backdrop or Escape without touching history', async () => {
  render(<App />);
  const trigger = await screen.findByRole('button', { name: '原会话 的菜单' });
  for (const action of ['escape', 'outside']) {
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('menuitem', { name: '删除会话' }));
    const dialog = screen.getByRole('dialog', { name: '删除会话' });
    expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus();
    if (action === 'escape') await userEvent.keyboard('{Escape}');
    else await userEvent.click(dialog.parentElement!);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(trigger).toHaveFocus();
  }
  expect(api.deleteSession).not.toHaveBeenCalled();
});

it('deletes the selected session, removes its local drafts and ignores late snapshots', async () => {
  localStorage.setItem(
    'mclaw.drafts.v1',
    JSON.stringify({ s1: '待删除的草稿', new: '保留的新草稿' }),
  );
  localStorage.setItem(
    'mclaw.pending-submissions.v1',
    JSON.stringify({ '["s1","queue","old"]': 'request' }),
  );
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  const emit = vi.mocked(subscribeSession).mock.calls[0][1];
  await userEvent.click(screen.getByRole('button', { name: '原会话 的菜单' }));
  await userEvent.click(screen.getByRole('menuitem', { name: '删除会话' }));
  expect(api.deleteSession).not.toHaveBeenCalled();
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '删除' }));
  await screen.findByRole('heading', { name: '今天，一起完成什么？' });
  expect(api.deleteSession).toHaveBeenCalledExactlyOnceWith('s1');
  expect(screen.queryByRole('button', { name: '原会话 的菜单' })).toBeNull();
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('保留的新草稿');
  expect(JSON.parse(localStorage.getItem('mclaw.drafts.v1')!)).not.toHaveProperty('s1');
  expect(JSON.parse(localStorage.getItem('mclaw.pending-submissions.v1')!)).toEqual({});
  await act(async () => emit({ type: 'snapshot', session: base }));
  expect(screen.queryByRole('button', { name: '原会话 的菜单' })).toBeNull();
});

it('retains the session and shows a retryable deletion error', async () => {
  vi.mocked(api.deleteSession).mockRejectedValueOnce(
    new Error('请先停止当前任务，等待收尾后再删除会话'),
  );
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: '原会话 的菜单' }));
  await userEvent.click(screen.getByRole('menuitem', { name: '删除会话' }));
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '删除' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('请先停止当前任务');
  expect(screen.getByRole('button', { name: '原会话 的菜单' })).toBeInTheDocument();
  expect(within(screen.getByRole('dialog')).getByRole('button', { name: '删除' })).toBeEnabled();
});

it('prevents duplicate deletion and leaves another selected conversation open', async () => {
  const other = { ...base, id: 's2', title: '第二会话' };
  vi.mocked(api.sessions).mockResolvedValue([base, other]);
  let resolve!: (result: { deleted: boolean; id: string }) => void;
  vi.mocked(api.deleteSession).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.click(screen.getByRole('button', { name: '第二会话 的菜单' }));
  await userEvent.click(screen.getByRole('menuitem', { name: '删除会话' }));
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '删除' }));
  expect(screen.getByRole('button', { name: '正在删除…' })).toBeDisabled();
  await userEvent.keyboard('{Escape}');
  expect(screen.getByRole('dialog')).toBeVisible();
  await act(async () => resolve({ deleted: true, id: 's2' }));
  expect(api.deleteSession).toHaveBeenCalledExactlyOnceWith('s2');
  expect(screen.getByText('原历史')).toBeVisible();
  expect(screen.queryByRole('button', { name: '第二会话 的菜单' })).toBeNull();
});

it('distinguishes scheduled and channel conversations in recent sessions and the active title', async () => {
  const scheduled = { ...base, id: 'schedule', title: '每日简报', source: 'scheduler' };
  vi.mocked(api.sessions).mockResolvedValue([
    scheduled,
    { ...base, id: 'wechat', title: '微信消息1', source: 'weixin' },
    { ...base, id: 'ding', title: '钉钉消息1', source: 'dingtalk' },
    base,
  ]);
  vi.mocked(api.session).mockResolvedValue(scheduled);
  render(<App />);
  const item = await screen.findByRole('button', { name: /每日简报$/ });
  expect(within(item).getByRole('img', { name: '定时任务' })).toHaveAttribute('title', '定时任务');
  expect(
    within(screen.getByRole('button', { name: /微信消息1$/ })).getByRole('img', { name: '微信' }),
  ).toBeVisible();
  expect(
    within(screen.getByRole('button', { name: /钉钉消息1$/ })).getByRole('img', { name: '钉钉' }),
  ).toBeVisible();
  expect(within(screen.getByRole('button', { name: /原会话$/ })).queryByRole('img')).toBeNull();
  await userEvent.click(item);
  expect(
    within(await screen.findByRole('heading', { name: /每日简报/ })).getByRole('img', {
      name: '定时任务',
    }),
  ).toBeVisible();
});

it('loads the home behind the intro and enables it after the crossfade', async () => {
  vi.useFakeTimers();
  const media = vi.spyOn(window, 'matchMedia').mockReturnValue({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  } as unknown as MediaQueryList);
  try {
    render(<App />);
    await act(async () => {});
    expect(api.bootstrap).toHaveBeenCalledOnce();
    expect(screen.getByText('今天，一起完成什么？').closest('.startup-content')).toHaveAttribute(
      'inert',
    );
    expect(screen.queryByRole('textbox', { name: '描述你的任务' })).not.toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(1200));
    expect(screen.getByRole('region', { name: '打开 M-Claw' })).toHaveClass('is-leaving');
    await act(async () => vi.advanceTimersByTimeAsync(240));
    expect(screen.queryByRole('region', { name: '打开 M-Claw' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toBeEnabled();
    expect(api.bootstrap).toHaveBeenCalledOnce();
  } finally {
    vi.useRealTimers();
    media.mockRestore();
  }
});

it('opens standalone provider management from settings without entering Setup', async () => {
  render(<App />);
  await screen.findByRole('textbox', { name: '描述你的任务' });
  await userEvent.click(screen.getByRole('button', { name: '打开导航' }));
  await userEvent.click(screen.getByRole('button', { name: '设置' }));
  expect(screen.queryByRole('button', { name: '关闭导航' })).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole('button', { name: '供应商与模型 添加供应商，管理 API Key 和默认模型' }),
  );
  expect(await screen.findByRole('dialog', { name: '新增供应商' })).toBeVisible();
  expect(api.setupStart).not.toHaveBeenCalled();
  expect(api.setupStatus).not.toHaveBeenCalled();
  expect(api.createSession).not.toHaveBeenCalled();
});

it('refreshes the model library from provider settings without creating a conversation or Setup run', async () => {
  const status = {
    updated_at: 1800000000,
    providers: 1,
    models: 1,
    refreshing: false,
    last_refresh: { ok: true, finished_at: 1800000000 },
  };
  vi.mocked(providersApi.refreshLibrary).mockResolvedValue(status);
  vi.mocked(providersApi.library).mockResolvedValue({
    status,
    providers: [],
    models: [],
    total: 0,
    offset: 0,
    limit: 100,
  });
  render(<App />);
  await screen.findByRole('textbox', { name: '描述你的任务' });
  await userEvent.click(screen.getByRole('button', { name: '设置' }));
  await userEvent.click(
    screen.getByRole('button', { name: '供应商与模型 添加供应商，管理 API Key 和默认模型' }),
  );
  await userEvent.click(await screen.findByRole('button', { name: '查看模型库' }));
  await userEvent.click(await screen.findByRole('button', { name: '刷新模型库' }));

  expect(await screen.findByRole('dialog', { name: '模型库' })).toBeVisible();
  await waitFor(() => expect(providersApi.refreshLibrary).toHaveBeenCalledOnce());
  expect(api.setupStart).not.toHaveBeenCalled();
  expect(api.createSession).not.toHaveBeenCalled();
});

it('fills a shortcut draft and refreshes the welcome on new chat without sending a task', async () => {
  render(<App />);
  await screen.findByRole('heading', { name: '今天，一起完成什么？' });
  const choices = () =>
    within(screen.getByRole('group', { name: '试试这些任务' })).getAllByRole('button');
  const first = choices().map((button) => button.textContent);
  const layout = screen
    .getByRole('button', { name: '和字母小伙伴打个招呼' })
    .getAttribute('data-layout');
  await userEvent.click(choices()[0]);
  const input = screen.getByRole('textbox', { name: '描述你的任务' });
  expect(input).toHaveValue(WELCOME_ACTIONS.find(({ title }) => title === first[0])!.text);
  expect(input).toHaveFocus();
  await userEvent.type(input, ' 这是补充');
  expect(choices().map((button) => button.textContent)).toEqual(first);
  await userEvent.click(screen.getByRole('button', { name: '新建会话' }));
  expect(
    choices()
      .map((button) => button.textContent)
      .sort(),
  ).not.toEqual([...first].sort());
  expect(screen.getByRole('button', { name: '和字母小伙伴打个招呼' })).not.toHaveAttribute(
    'data-layout',
    layout,
  );
  expect((input as HTMLTextAreaElement).value).toContain('这是补充');
  expect(api.createSession).not.toHaveBeenCalled();
  expect(api.submit).not.toHaveBeenCalled();
});

it('quietens the companions for focus or a saved draft while retaining slash commands and shortcuts', async () => {
  localStorage.setItem('mclaw.drafts.v1', JSON.stringify({ new: '未发送的想法' }));
  render(<App />);
  const actor = await screen.findByRole('button', { name: '和字母小伙伴打个招呼' });
  const input = screen.getByRole('textbox', { name: '描述你的任务' });
  expect(actor).toHaveAttribute('data-quiet', 'true');
  const layout = actor.getAttribute('data-layout');
  await userEvent.clear(input);
  expect(actor).toHaveAttribute('data-quiet', 'true');
  await userEvent.tab();
  expect(actor).toHaveAttribute('data-quiet', 'false');
  await userEvent.type(input, '/');
  expect(await screen.findByRole('listbox', { name: '操作与技能' })).toBeVisible();
  expect(actor).toHaveAttribute('data-quiet', 'true');
  expect(actor).toHaveAttribute('data-layout', layout);
  await userEvent.click(screen.getByRole('heading', { name: '今天，一起完成什么？' }));
  expect(actor).toHaveAttribute('data-quiet', 'true');
  expect(screen.queryByRole('listbox', { name: '操作与技能' })).not.toBeInTheDocument();
  expect(api.submit).not.toHaveBeenCalled();
});

it('keeps the welcome after a failed first submission and unmounts it after acceptance', async () => {
  const empty = { ...base, id: 'new-session', messages: [] };
  vi.mocked(api.createSession).mockResolvedValue(empty);
  vi.mocked(api.session).mockResolvedValue(empty);
  let rejectSubmission!: (error: Error) => void;
  vi.mocked(api.submit)
    .mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectSubmission = reject;
        }),
    )
    .mockResolvedValueOnce({
      ...empty,
      status: 'running',
      messages: [{ id: 'm1', role: 'user', content: '新任务' }],
    });
  render(<App />);
  await userEvent.type(await screen.findByRole('textbox', { name: '描述你的任务' }), '新任务');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(api.session).toHaveBeenCalledWith(empty.id));
  await act(async () => rejectSubmission(new Error('测试提交失败')));
  expect(await screen.findByText('测试提交失败')).toBeVisible();
  expect(screen.getByRole('button', { name: '和字母小伙伴打个招呼' })).toBeVisible();
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('新任务');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: '和字母小伙伴打个招呼' })).not.toBeInTheDocument(),
  );
  expect(screen.getByText('新任务')).toBeVisible();
});

it('keeps connection failures actionable and clears the old error while retrying', async () => {
  vi.mocked(api.bootstrap).mockRejectedValueOnce(new Error('本机服务暂时没有响应'));
  render(<App />);
  expect(await screen.findByRole('alert')).toHaveTextContent('本机服务暂时没有响应');
  let resolve!: (value: Awaited<ReturnType<typeof api.bootstrap>>) => void;
  vi.mocked(api.bootstrap).mockReturnValueOnce(new Promise((done) => (resolve = done)));
  await userEvent.click(screen.getByRole('button', { name: '重新连接' }));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('正在连接本机服务');
  await act(async () =>
    resolve({
      setup_required: false,
      version: 'test',
      device: { name: '测试设备', platform: 'windows' },
      models: [],
      capabilities: [],
    }),
  );
  expect(await screen.findByRole('textbox', { name: '描述你的任务' })).toBeEnabled();
});

it('keeps conversation slash commands and links help to the manual', async () => {
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  const input = screen.getByRole('textbox', { name: '描述你的任务' });
  expect(screen.getByRole('link', { name: '帮助' })).toHaveAttribute(
    'href',
    'https://gitcode.com/m-robots/mclaw/blob/main/docs/manual/README.md',
  );
  await userEvent.type(input, '/');
  expect(
    screen.queryByRole('option', { name: /provider|settings|devices|schedule|model-update|help/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole('option', { name: /history/ })).toBeVisible();
  await userEvent.clear(input);
  await userEvent.type(input, '/history{Enter}');
  expect(screen.getByRole('textbox', { name: '搜索会话' })).toHaveFocus();
  expect(api.createSession).not.toHaveBeenCalled();
  expect(api.submit).not.toHaveBeenCalled();
});

it('keeps the existing new-session draft when /clear leaves a historical conversation', async () => {
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '新会话草稿');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '/clear{Enter}');
  expect(screen.getByText('今天，一起完成什么？')).toBeVisible();
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('新会话草稿');
  expect(screen.getByRole('button', { name: '准备就绪 原会话' })).toBeVisible();
  expect(api.submit).not.toHaveBeenCalled();
});

it('continues a paused task without treating the shortcut as a supplement', async () => {
  const paused = { ...base, status: 'interrupted' as const, active_task: { id: 't1' } };
  vi.mocked(api.session).mockResolvedValue(paused);
  vi.mocked(api.resume).mockResolvedValue({ ...paused, status: 'running' });
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '/continue{Enter}');
  await waitFor(() => expect(api.resume).toHaveBeenCalledExactlyOnceWith('s1'));
  expect(api.submit).not.toHaveBeenCalled();
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('');
});

it('changes the saved session model from the slash menu through the existing service', async () => {
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '/model{Enter}');
  await userEvent.click(
    within(screen.getByRole('listbox', { name: '选择模型' })).getByRole('option', {
      name: /测试模型/,
    }),
  );
  expect(api.updateSession).toHaveBeenCalledWith('s1', { model: 'test-model' });
  expect(api.submit).not.toHaveBeenCalled();
});

it('does not overwrite a newer live snapshot with a delayed historical response', async () => {
  let resolveHistory!: (value: Session) => void;
  let receive!: (event: SessionEvent) => void;
  vi.mocked(api.session).mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
  );
  vi.mocked(subscribeSession).mockImplementation((_id, callback) => {
    receive = callback;
    return () => {};
  });
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await waitFor(() => expect(api.session).toHaveBeenCalled());
  await act(async () => {
    receive({
      type: 'snapshot',
      seq: 2,
      session: {
        ...base,
        seq: 2,
        messages: [{ id: 'new', role: 'assistant', content: '最新正文' }],
      },
    });
    resolveHistory({ ...base, seq: 1 });
  });
  expect(screen.getByText('最新正文')).toBeVisible();
  expect(screen.queryByText('原历史')).not.toBeInTheDocument();
});

it('shows one live running status and updates it without duplicating a conversation footer', async () => {
  let receive!: (event: SessionEvent) => void;
  const running: Session = { ...base, status: 'running', status_text: '等待回复…' };
  vi.mocked(api.session).mockResolvedValue(running);
  vi.mocked(subscribeSession).mockImplementation((_id, callback) => {
    receive = callback;
    return () => {};
  });
  render(<App />);
  await userEvent.click(await screen.findByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  expect(screen.getAllByText('等待回复…')).toHaveLength(1);
  expect(screen.getByRole('status')).toHaveTextContent('等待回复…');
  await act(async () =>
    receive({
      type: 'snapshot',
      seq: 2,
      session: { ...running, seq: 2, status_text: '整理上下文…' },
    }),
  );
  expect(screen.queryByText('等待回复…')).not.toBeInTheDocument();
  expect(screen.getAllByText('整理上下文…')).toHaveLength(1);
  expect(screen.getByRole('status')).toHaveTextContent('整理上下文…');
  await act(async () =>
    receive({
      type: 'snapshot',
      seq: 3,
      session: { ...running, seq: 3, status_text: '' },
    }),
  );
  expect(screen.getByRole('status')).toHaveTextContent('处理中…');
});

it('opens on welcome, preserves per-session drafts and keeps historical conversations', async () => {
  render(<App />);
  expect(await screen.findByText('今天，一起完成什么？')).toBeVisible();
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '新会话草稿');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '历史会话草稿');
  await userEvent.click(screen.getByRole('button', { name: /新建会话/ }));
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('新会话草稿');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('历史会话草稿'),
  );
});

it('preserves a draft when the server rejects submission and clears it only after acceptance', async () => {
  localStorage.setItem('mclaw.draft-skills.v1', JSON.stringify({ s1: ['weekly'] }));
  vi.mocked(api.submit)
    .mockRejectedValueOnce(new Error('测试提交失败'))
    .mockResolvedValueOnce({
      ...base,
      status: 'running',
      messages: [...base.messages, { id: 'm2', role: 'user', content: '保留任务' }],
    });
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), '保留任务');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  expect(await screen.findByText('测试提交失败')).toBeVisible();
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('保留任务');
  expect(screen.getByRole('button', { name: '移除技能 weekly' })).toBeVisible();
  expect(vi.mocked(api.submit).mock.calls[0][4]).toEqual(['weekly']);
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue(''),
  );
  expect(vi.mocked(api.submit).mock.calls[0][3]).toEqual(vi.mocked(api.submit).mock.calls[1][3]);
  expect(screen.queryByRole('button', { name: '移除技能 weekly' })).not.toBeInTheDocument();
});

it('retains a pending submission ID after another session sends and the page reloads', async () => {
  const other: Session = {
    ...base,
    id: 's2',
    title: '第二会话',
    messages: [{ id: 'history-b', role: 'user', content: '第二历史' }],
  };
  vi.mocked(api.sessions).mockResolvedValue([base, other]);
  vi.mocked(api.session).mockImplementation(async (id) => (id === base.id ? base : other));
  vi.mocked(api.submit)
    .mockRejectedValueOnce(new Error('响应丢失'))
    .mockResolvedValueOnce({ ...other, status: 'running' })
    .mockResolvedValueOnce({ ...base, status: 'running' });
  const first = render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), 'A任务');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await screen.findByText('响应丢失');
  const firstId = vi.mocked(api.submit).mock.calls[0][3];
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 第二会话' }));
  await screen.findByText('第二历史');
  await userEvent.type(screen.getByRole('textbox', { name: '描述你的任务' }), 'B任务');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue(''),
  );
  first.unmount();
  render(<App />);
  await screen.findByText('今天，一起完成什么？');
  await userEvent.click(screen.getByRole('button', { name: '准备就绪 原会话' }));
  await screen.findByText('原历史');
  expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('A任务');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(3));
  expect(vi.mocked(api.submit).mock.calls[2][3]).toBe(firstId);
});
