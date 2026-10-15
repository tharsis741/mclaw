import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { Setup } from './Setup';
import { api } from '../api';
import type { SetupRun } from '../types';

vi.mock('../api', () => ({
  api: {
    setupStatus: vi.fn(),
    setupStart: vi.fn(),
    setupRun: vi.fn(),
    setupSubmit: vi.fn(),
    setupCancel: vi.fn(),
  },
}));
const run: SetupRun = {
  id: 'run1',
  status: 'waiting',
  prompt: {
    id: 'prompt1',
    kind: 'single_select',
    title: '选择接口 Profile',
    default: ['main'],
    options: [
      { id: 'main', label: '标准接口' },
      { id: 'other', label: '其他接口' },
    ],
  },
  messages: [],
  saved: false,
  configuration: { ready: false, required: true },
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.setupStatus).mockResolvedValue({
    required: true,
    ready: false,
    providers: [],
    steps: [],
  });
  vi.mocked(api.setupStart).mockResolvedValue(run);
});

it('moves focus to the prepared form only after the startup screen is gone', async () => {
  const onComplete = vi.fn();
  const { rerender } = render(<Setup active={false} onComplete={onComplete} />);
  const heading = await screen.findByRole('heading', { name: '选择接口 Profile' });
  expect(heading).not.toHaveFocus();
  rerender(<Setup active onComplete={onComplete} />);
  expect(heading).toHaveFocus();
  expect(api.setupStart).toHaveBeenCalledOnce();
});

it('cancels the backend workflow before explicitly closing a waiting setup', async () => {
  let cancel!: (value: SetupRun) => void;
  vi.mocked(api.setupCancel).mockReturnValueOnce(
    new Promise((done) => {
      cancel = done;
    }),
  );
  const onClose = vi.fn();
  render(<Setup onComplete={vi.fn()} onClose={onClose} />);
  await screen.findByRole('heading', { name: '选择接口 Profile' });
  await userEvent.click(screen.getByRole('button', { name: '关闭设置' }));
  expect(api.setupCancel).toHaveBeenCalledWith('run1');
  expect(onClose).not.toHaveBeenCalled();
  await act(async () => cancel({ ...run, status: 'cancelled', prompt: null }));
  expect(onClose).toHaveBeenCalledOnce();
});

it('cancels a newly created setup that arrives after its page has gone away', async () => {
  let start!: (value: SetupRun) => void;
  vi.mocked(api.setupStart).mockReturnValueOnce(
    new Promise((done) => {
      start = done;
    }),
  );
  vi.mocked(api.setupCancel).mockResolvedValue({ ...run, status: 'cancelled', prompt: null });
  const { unmount } = render(<Setup onComplete={vi.fn()} />);
  await waitFor(() => expect(api.setupStart).toHaveBeenCalled());
  unmount();
  await act(async () => start(run));
  expect(api.setupCancel).toHaveBeenCalledWith('run1');
});

it('renders server-selected defaults and submits the selected provider profile', async () => {
  vi.mocked(api.setupSubmit).mockResolvedValue({
    ...run,
    status: 'completed',
    prompt: null,
    saved: true,
    configuration: { ready: true, required: false, provider: 'test', model: 'test-model' },
  });
  render(<Setup onComplete={vi.fn()} />);
  expect(await screen.findByRole('radio', { name: '标准接口' })).toBeChecked();
  await userEvent.click(screen.getByRole('radio', { name: '其他接口' }));
  await userEvent.click(screen.getByRole('button', { name: '下一步' }));
  expect(api.setupSubmit).toHaveBeenCalledWith('run1', 'prompt1', 'other');
  expect(await screen.findByText('配置已完成')).toBeVisible();
});

it('shows the permission explanation with an explicit consent button and no terminal log', async () => {
  vi.mocked(api.setupStart).mockResolvedValue({
    ...run,
    messages: ['TUI terminal output', '配置说明与保存记录'],
    prompt: {
      id: 'welcome',
      kind: 'single_select',
      title: '开始使用 M-Claw',
      presentation: 'welcome',
      credential_path: 'test-home/.env',
      options: [
        { id: 'y', label: '同意并开始配置' },
        { id: 'n', label: '暂不配置' },
      ],
      default: ['y'],
    },
  });
  vi.mocked(api.setupSubmit).mockResolvedValue(run);
  render(<Setup onComplete={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: '开始使用 M-Claw' })).toBeVisible();
  expect(screen.getByText('你的文件')).toBeVisible();
  expect(screen.getByText('模型服务')).toBeVisible();
  expect(screen.getByText('test-home/.env')).toBeVisible();
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  expect(screen.queryByText('配置说明与保存记录')).not.toBeInTheDocument();
  expect(screen.queryByText('TUI terminal output')).not.toBeInTheDocument();
  expect(api.setupSubmit).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '同意并开始配置' }));
  expect(api.setupSubmit).toHaveBeenCalledWith('run1', 'welcome', 'y');
});

it('selects and clears all capabilities without submitting, using different tool icons', async () => {
  const options = [
    { id: 'web_extract', label: '网页提取' },
    { id: 'web_search', label: '网页搜索' },
    { id: 'vision', label: '视觉分析' },
    { id: 'browser', label: '浏览器自动化' },
  ];
  vi.mocked(api.setupStart).mockResolvedValue({
    ...run,
    prompt: {
      id: 'tools',
      kind: 'multi_select',
      presentation: 'capabilities',
      title: '可选能力',
      options,
      default: ['vision'],
    },
  });
  vi.mocked(api.setupSubmit).mockResolvedValue(run);
  const { container } = render(<Setup onComplete={vi.fn()} />);
  expect(await screen.findByRole('checkbox', { name: '视觉分析' })).toBeChecked();
  const icons = [...container.querySelectorAll('.setup-option-icon svg')].map(
    (icon) => icon.innerHTML,
  );
  expect(new Set(icons).size).toBe(4);
  await userEvent.click(screen.getByRole('button', { name: '全选' }));
  screen.getAllByRole('checkbox').forEach((checkbox) => expect(checkbox).toBeChecked());
  expect(screen.getByText('已选 4 项')).toBeVisible();
  expect(api.setupSubmit).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '取消全选' }));
  screen.getAllByRole('checkbox').forEach((checkbox) => expect(checkbox).not.toBeChecked());
  await userEvent.click(screen.getByRole('button', { name: '全选' }));
  await userEvent.click(screen.getByRole('button', { name: '下一步' }));
  expect(api.setupSubmit).toHaveBeenCalledWith(
    'run1',
    'tools',
    options.map((item) => item.id),
  );
  expect(await screen.findByRole('radio', { name: '标准接口' })).toBeChecked();
  expect(screen.queryByRole('button', { name: '全选' })).not.toBeInTheDocument();
});

it('keeps the form visible and disabled while fetching the next step immediately', async () => {
  let next!: (value: SetupRun) => void;
  vi.mocked(api.setupSubmit).mockResolvedValue({ ...run, status: 'running', prompt: null });
  vi.mocked(api.setupRun).mockImplementation(
    () =>
      new Promise((resolve) => {
        next = resolve;
      }),
  );
  render(<Setup onComplete={vi.fn()} />);
  await screen.findByRole('radio', { name: '标准接口' });
  await userEvent.click(screen.getByRole('button', { name: '下一步' }));
  await waitFor(() => expect(api.setupRun).toHaveBeenCalledWith('run1'));
  expect(screen.getByRole('radio', { name: '标准接口' })).toBeVisible();
  expect(screen.getByRole('radio', { name: '标准接口' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '退出配置' })).toBeEnabled();
  await act(async () =>
    next({
      ...run,
      prompt: { id: 'key', kind: 'secret', title: '连接供应商', field_label: 'API Key' },
    }),
  );
  expect(screen.getByLabelText('API Key')).toBeEnabled();
  expect(screen.getByLabelText('API Key')).toHaveFocus();
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
});

it('recovers the accepted step after a lost submit response instead of submitting twice', async () => {
  vi.mocked(api.setupSubmit).mockRejectedValue(new Error('连接中断'));
  vi.mocked(api.setupRun).mockResolvedValue({
    ...run,
    prompt: { id: 'key', kind: 'secret', title: '填写 API Key' },
  });
  render(<Setup onComplete={vi.fn()} />);
  await screen.findByRole('radio', { name: '标准接口' });
  await userEvent.click(screen.getByRole('button', { name: '下一步' }));
  expect(await screen.findByLabelText('密钥')).toBeEnabled();
  expect(api.setupSubmit).toHaveBeenCalledTimes(1);
});

it('can cancel a long operation and ignores the older poll response', async () => {
  let next!: (value: SetupRun) => void;
  vi.mocked(api.setupSubmit).mockResolvedValue({ ...run, status: 'running', prompt: null });
  vi.mocked(api.setupRun).mockImplementation(
    () =>
      new Promise((resolve) => {
        next = resolve;
      }),
  );
  vi.mocked(api.setupCancel).mockResolvedValue({
    ...run,
    status: 'cancelled',
    prompt: null,
    saved: true,
  });
  render(<Setup onComplete={vi.fn()} />);
  await screen.findByRole('radio', { name: '标准接口' });
  await userEvent.click(screen.getByRole('button', { name: '下一步' }));
  await waitFor(() => expect(api.setupRun).toHaveBeenCalled());
  await userEvent.click(screen.getByRole('button', { name: '退出配置' }));
  expect(await screen.findByText('已退出配置')).toBeVisible();
  await act(async () => next(run));
  expect(screen.getByText('已退出配置')).toBeVisible();
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
});

it('reconnects an unfinished setup instead of starting another run', async () => {
  vi.mocked(api.setupStatus).mockResolvedValue({
    required: true,
    ready: false,
    providers: [],
    steps: [],
    active_run_id: 'run1',
  });
  vi.mocked(api.setupRun).mockResolvedValue({
    ...run,
    prompt: { id: 'key', kind: 'secret', title: '配置 Key', secret_configured: true },
  });
  render(<Setup onComplete={vi.fn()} />);
  expect(await screen.findByLabelText('密钥')).toHaveAttribute('type', 'password');
  expect(screen.getByText(/已保存凭据/)).toBeVisible();
  expect(api.setupStart).not.toHaveBeenCalled();
});

it('shows actionable warnings without terminal output or a meaningless selection', async () => {
  vi.mocked(api.setupStart).mockResolvedValue({
    ...run,
    prompt: {
      id: 'notice',
      kind: 'single_select',
      presentation: 'notice',
      title: '这个接口暂时无法配置',
      hint: '请使用自定义接口。',
      options: [{ id: 'continue', label: '下一步' }],
      default: ['continue'],
    },
  });
  vi.mocked(api.setupSubmit).mockResolvedValue(run);
  render(<Setup onComplete={vi.fn()} />);
  expect(await screen.findByText('请使用自定义接口。')).toBeVisible();
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '下一步' }));
  expect(api.setupSubmit).toHaveBeenCalledWith('run1', 'notice', 'continue');
});
