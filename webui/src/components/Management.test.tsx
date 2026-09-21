import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { ApiError, schedulesApi, settingsApi, skillsApi } from '../api';
import { SkillsPage, sourceLabel } from './SkillsPage';
import { SchedulesPage } from './SchedulesPage';
import { SettingsPage } from './SettingsPage';
import { SkillInstallRequest } from './SkillInstallRequest';
import type { Bootstrap, MemoryDocument, ScheduledJob, SkillDetail } from '../types';

vi.mock('../api', async () => ({
  ...(await vi.importActual<typeof import('../api')>('../api')),
  skillsApi: {
    list: vi.fn(),
    detail: vi.fn(),
    update: vi.fn(),
    create: vi.fn(),
    prepare: vi.fn(),
    confirm: vi.fn(),
    cancel: vi.fn(),
  },
  schedulesApi: {
    list: vi.fn(),
    targets: vi.fn(),
    preview: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    enabled: vi.fn(),
    run: vi.fn(),
    runs: vi.fn(),
    cancelRun: vi.fn(),
  },
  settingsApi: {
    memory: vi.fn(),
    saveMemory: vi.fn(),
    diagnostics: vi.fn(),
    configuration: vi.fn().mockResolvedValue({ revision: 'r1', groups: [] }),
    saveConfiguration: vi.fn(),
  },
}));
const skill: SkillDetail = {
  name: 'weekly',
  description: '整理周报',
  enabled: true,
  source: { type: 'local', original: 'C:/skills/weekly' },
  version: 'version-1',
  content: '---\nname: weekly\ndescription: 周报\n---\n# 周报\n整理摘要',
  tree: { entries: [] },
};
const job: ScheduledJob = {
  id: 'job-1',
  name: '每天摘要',
  prompt: '整理工作摘要',
  schedule: {
    trigger_type: 'daily',
    timezone: 'Asia/Shanghai',
    expression: '09:00',
    parsed: { hour: 9, minute: 0 },
  },
  enabled: true,
  status: 'enabled',
  session_id: 's1',
  created_at: 1,
  session_policy: 'task_thread',
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(skillsApi.list).mockResolvedValue({ skills: [skill] });
  vi.mocked(skillsApi.detail).mockResolvedValue(skill);
  vi.mocked(skillsApi.update).mockResolvedValue(skill);
  vi.mocked(schedulesApi.list).mockResolvedValue({
    items: [job],
    total: 1,
    offset: 0,
    limit: 50,
    runtime: { running: true },
    defaults: { timezone: 'UTC', max_iterations: 321, timeout_seconds: 7200 },
  });
  vi.mocked(schedulesApi.targets).mockResolvedValue({
    items: [
      { id: 'local', type: 'local', display_name: '本机', enabled: true, route_status: 'ready' },
    ],
  });
  vi.mocked(schedulesApi.update).mockResolvedValue(job);
  vi.mocked(schedulesApi.preview).mockResolvedValue({
    next_runs: [1800000000, 1800086400, 1800172800],
    schedule_label: '每天 10:30',
  });
});

it('uses shared configuration defaults in the new schedule editor', async () => {
  render(<SchedulesPage onSession={vi.fn()} />);
  await screen.findByText('每天摘要');
  await userEvent.click(screen.getByRole('button', { name: /新建/ }));
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByRole('combobox', { name: '时区' })).toHaveValue('UTC');
  await userEvent.click(within(dialog).getByText('工作区、执行方式与投递'));
  expect(within(dialog).getByRole('spinbutton', { name: '最长运行时间（秒）' })).toHaveValue(7200);
  expect(within(dialog).getByRole('spinbutton', { name: '最多执行轮数' })).toHaveValue(321);
});

it('uses the skill read version when editing and keeps its editor on rejection', async () => {
  vi.mocked(skillsApi.update).mockRejectedValueOnce(new Error('内容已经更新，请重新读取'));
  render(<SkillsPage onUse={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: /weekly 整理周报/ }));
  const drawer = screen.getByRole('dialog');
  expect(within(drawer).queryByText(/name: weekly/)).not.toBeInTheDocument();
  await userEvent.click(within(drawer).getByRole('button', { name: '编辑技能内容' }));
  const editor = within(drawer).getByRole('textbox');
  await userEvent.clear(editor);
  await userEvent.type(editor, '# 新方法');
  await userEvent.click(within(drawer).getByRole('button', { name: /保存/ }));
  await waitFor(() =>
    expect(skillsApi.update).toHaveBeenCalledWith('weekly', {
      content: '# 新方法',
      version: 'version-1',
    }),
  );
  expect(editor).toHaveValue('# 新方法');
});

it('edits the brief description separately and refreshes the list and read version', async () => {
  const updated = { ...skill, description: '汇总进展和待办', version: 'version-2' };
  vi.mocked(skillsApi.update).mockResolvedValueOnce(updated);
  render(<SkillsPage onUse={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: /weekly 整理周报/ }));
  const drawer = screen.getByRole('dialog');
  await userEvent.click(within(drawer).getByRole('button', { name: '编辑简短描述' }));
  const editor = within(drawer).getByLabelText('简短描述');
  expect(editor).toHaveValue('整理周报');
  expect(within(drawer).getByRole('button', { name: '保存描述' })).toBeDisabled();
  await userEvent.clear(editor);
  expect(within(drawer).getByRole('button', { name: '保存描述' })).toBeDisabled();
  await userEvent.type(editor, '  汇总进展和待办  ');
  await userEvent.click(within(drawer).getByRole('button', { name: '保存描述' }));
  await screen.findByRole('button', { name: /weekly 汇总进展和待办/ });
  expect(skillsApi.update).toHaveBeenCalledWith('weekly', {
    description: '汇总进展和待办',
    version: 'version-1',
  });
  expect(within(drawer).queryByLabelText('简短描述')).not.toBeInTheDocument();
  await userEvent.click(within(drawer).getByRole('button', { name: '编辑技能内容' }));
  expect(within(drawer).getByLabelText('技能内容')).toHaveValue(skill.content);
  await userEvent.click(within(drawer).getByRole('button', { name: '保存修改' }));
  await waitFor(() =>
    expect(skillsApi.update).toHaveBeenLastCalledWith('weekly', {
      content: skill.content,
      version: 'version-2',
    }),
  );
});

it('keeps a rejected description draft and cancels without saving it', async () => {
  vi.mocked(skillsApi.update).mockRejectedValueOnce(
    new Error('技能已变化，请重新读取并核对后保存'),
  );
  render(<SkillsPage onUse={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: /weekly 整理周报/ }));
  const drawer = screen.getByRole('dialog');
  await userEvent.click(within(drawer).getByRole('button', { name: '编辑简短描述' }));
  const editor = within(drawer).getByLabelText('简短描述');
  await userEvent.clear(editor);
  await userEvent.type(editor, '保留这份草稿');
  expect(within(drawer).getByRole('button', { name: '编辑技能内容' })).toBeDisabled();
  await userEvent.click(within(drawer).getByRole('button', { name: '保存描述' }));
  await within(drawer).findByRole('alert');
  expect(editor).toHaveValue('保留这份草稿');
  await userEvent.click(within(drawer).getByRole('button', { name: '取消' }));
  expect(within(drawer).getByText('整理周报')).toBeVisible();
  expect(skillsApi.update).toHaveBeenCalledOnce();
  await userEvent.click(within(drawer).getByRole('button', { name: '编辑简短描述' }));
  expect(within(drawer).getByLabelText('简短描述')).toHaveValue('整理周报');
});

it('labels a local source even when its original value is empty', () => {
  expect(sourceLabel({ type: 'local', original: '' })).toBe('本机');
  expect(sourceLabel({ type: 'agent_created', original: '' })).toBe('本机创建');
  expect(sourceLabel({ type: 'unknown_origin', original: '' })).toBe('其他来源');
  expect(sourceLabel({ type: 'unknown_origin', original: '' }, true)).toBe('unknown_origin');
});

it('prepares an install and confirms only the reviewed token', async () => {
  vi.mocked(skillsApi.prepare).mockResolvedValue({
    drafting_id: 'draft',
    skill_name: 'weekly',
    source: 'C:/review',
    content: '方法说明',
    review: { summary: '可以安装' },
    blocked: false,
    confirmation_token: 'review-token',
  });
  render(<SkillsPage onUse={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: '安装技能' }));
  await userEvent.type(screen.getByLabelText('来源'), 'C:/review');
  await userEvent.click(screen.getByRole('button', { name: '检查并准备安装' }));
  await screen.findByText('可以安装');
  expect(skillsApi.confirm).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '确认安装' }));
  await waitFor(() => expect(skillsApi.confirm).toHaveBeenCalledWith('draft', 'review-token'));
});

it('routes an in-task installation confirmation through the task authorization', async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  render(
    <SkillInstallRequest
      request={{
        id: 'request',
        kind: 'skill_install',
        title: '安装技能',
        skill_name: 'weekly',
        content: '说明',
        blocked: false,
        confirmation_token: 'token',
      }}
      busy={false}
      onReply={onReply}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: /查看/ }));
  await userEvent.click(screen.getByRole('button', { name: '确认安装并继续' }));
  expect(onReply).toHaveBeenCalledWith({ approved: true, confirmation_token: 'token' });
  expect(skillsApi.confirm).not.toHaveBeenCalled();
});

it('previews an edited time and excludes immutable fields from the update', async () => {
  render(<SchedulesPage onSession={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '编辑 每天摘要' }));
  const dialog = screen.getByRole('dialog');
  const time = within(dialog).getByLabelText('时间');
  await userEvent.clear(time);
  await userEvent.type(time, '10:30');
  await userEvent.click(within(dialog).getByRole('button', { name: /预览/ }));
  await waitFor(() =>
    expect(schedulesApi.preview).toHaveBeenCalledWith(expect.objectContaining({ time: '10:30' })),
  );
  await userEvent.click(within(dialog).getByRole('button', { name: '保存任务' }));
  await waitFor(() => expect(schedulesApi.update).toHaveBeenCalled());
  const body = vi.mocked(schedulesApi.update).mock.calls[0][1];
  expect(body).toMatchObject({ name: '每天摘要', schedule: { time: '10:30' } });
  expect(body).not.toHaveProperty('id');
  expect(body).not.toHaveProperty('status');
  expect(body).not.toHaveProperty('session_id');
});

it('shows paused schedules without implying a next run and localizes weekdays', async () => {
  vi.mocked(schedulesApi.list).mockResolvedValue({
    items: [
      {
        ...job,
        enabled: false,
        next_run_at: 1800000000,
        schedule_label: '每周 Monday 09:00 Asia/Shanghai',
      },
    ],
    total: 1,
    offset: 0,
    limit: 50,
    runtime: { running: true },
    defaults: { timezone: 'UTC', max_iterations: 321, timeout_seconds: 7200 },
  });
  vi.mocked(schedulesApi.runs).mockResolvedValue({ items: [] });
  render(<SchedulesPage onSession={vi.fn()} />);
  const task = await screen.findByRole('button', { name: /每天摘要 整理工作摘要/ });
  const row = task.closest('tr')!;
  expect(within(row).getByText('已暂停')).toBeVisible();
  expect(within(row).getByText('—')).toBeVisible();
  expect(within(row).getByText('每周一 09:00 Asia/Shanghai')).toBeVisible();
  await userEvent.click(task);
  expect(within(screen.getByRole('dialog')).getByText('每周一 09:00 Asia/Shanghai')).toBeVisible();
});

it('preserves local memory edits on conflict and uses the latest revision only after explicit merge', async () => {
  const doc: MemoryDocument = {
    target: 'memory',
    label: '长期记忆内容',
    content: '旧内容',
    revision: 'r1',
    limit: 2000,
    history: [],
  };
  vi.mocked(settingsApi.memory).mockResolvedValue({ documents: [doc] });
  vi.mocked(settingsApi.saveMemory)
    .mockRejectedValueOnce(
      new ApiError('MEMORY_CONFLICT', '内容已更新', 409, {
        ...doc,
        content: '另一窗口内容',
        revision: 'r2',
      }),
    )
    .mockResolvedValueOnce({ ...doc, content: '合并内容', revision: 'r3' });
  render(
    <SettingsPage
      bootstrap={
        { device: { name: '本机', platform: 'windows' }, capabilities: [] } as unknown as Bootstrap
      }
      onSetup={vi.fn()}
      onProviders={vi.fn()}
      onShutdown={vi.fn()}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: '记忆与偏好' }));
  const editor = await screen.findByLabelText('长期记忆内容');
  await userEvent.clear(editor);
  await userEvent.type(editor, '本地编辑');
  await userEvent.click(screen.getByRole('button', { name: '保存修改' }));
  expect(await screen.findByLabelText('最新保存的内容')).toHaveValue('另一窗口内容');
  expect(editor).toHaveValue('本地编辑');
  await userEvent.clear(editor);
  await userEvent.type(editor, '合并内容');
  await userEvent.click(screen.getByRole('button', { name: '我已合并，重新保存' }));
  await waitFor(() =>
    expect(settingsApi.saveMemory).toHaveBeenLastCalledWith('memory', '合并内容', 'r2'),
  );
});
