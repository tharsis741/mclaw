import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { ApiError, toolsApi } from '../api';
import type { ToolCatalog } from '../types';
import { ToolsPage } from './ToolsPage';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  toolsApi: { catalog: vi.fn(), save: vi.fn() },
}));
const catalog: ToolCatalog = {
  revision: 'one',
  items: [
    {
      id: 'terminal',
      label: '终端',
      description: '执行命令，管理后台进程',
      required: true,
      enabled: true,
      ready: true,
      tools: ['terminal', 'process'],
      backends: [],
      backend: '',
      credential_source: '',
      model: '',
      channel: '',
    },
    {
      id: 'web_search',
      label: '网页搜索',
      description: '搜索最新资料和信息',
      required: false,
      enabled: false,
      ready: false,
      tools: ['web_search'],
      backends: [
        {
          id: 'tavily',
          label: 'Tavily',
          credentials: [{ id: 'TAVILY_API_KEY', configured: false }],
        },
        {
          id: 'dashscope',
          label: '通义千问 · 中国区',
          credentials: [{ id: 'DASHSCOPE_API_KEY', configured: true }],
        },
      ],
      backend: 'tavily',
      credential_source: '',
      model: '',
      channel: '',
    },
    {
      id: 'browser',
      label: '浏览器',
      description: '访问网页、填写表单和下载文件',
      required: false,
      enabled: true,
      ready: true,
      tools: ['browser_download'],
      backends: [],
      backend: '',
      credential_source: '',
      model: '',
      channel: '',
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(toolsApi.catalog).mockResolvedValue(structuredClone(catalog));
  vi.mocked(toolsApi.save).mockResolvedValue({ ...catalog, revision: 'two' });
});

it('locks required tools on and persists optional switches', async () => {
  render(<ToolsPage />);
  const required = await screen.findByRole('switch', { name: '终端开关' });
  expect(required).toBeChecked();
  expect(required).toBeDisabled();
  await userEvent.click(screen.getByRole('switch', { name: '浏览器开关' }));
  expect(toolsApi.save).toHaveBeenCalledWith('browser', { revision: 'one', enabled: false });
});

it('opens configuration before enabling a tool that needs a credential', async () => {
  render(<ToolsPage />);
  await userEvent.click(await screen.findByRole('switch', { name: '网页搜索开关' }));
  const dialog = await screen.findByRole('dialog', { name: '网页搜索' });
  expect(toolsApi.save).not.toHaveBeenCalled();
  expect(within(dialog).getByRole('button', { name: '保存' })).toBeDisabled();
  await userEvent.type(within(dialog).getByLabelText('API Key'), 'fixture-entered-key');
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(toolsApi.save).toHaveBeenCalledWith('web_search', {
    revision: 'one',
    enabled: true,
    backend: 'tavily',
    credential_source: 'TAVILY_API_KEY',
    api_key: 'fixture-entered-key',
  });
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
});

it('switches backend and reuses a saved key without exposing its value', async () => {
  render(<ToolsPage />);
  await userEvent.click(await screen.findByRole('button', { name: /网页搜索 搜索最新资料/ }));
  const dialog = await screen.findByRole('dialog', { name: '网页搜索' });
  await userEvent.click(within(dialog).getByRole('combobox', { name: '服务来源' }));
  await userEvent.click(screen.getByRole('option', { name: '通义千问 · 中国区' }));
  expect(within(dialog).getByLabelText('API Key')).toHaveValue('');
  expect(within(dialog).getByLabelText('API Key')).toHaveAttribute(
    'placeholder',
    '已保存，留空复用',
  );
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(toolsApi.save).toHaveBeenCalledWith('web_search', {
    revision: 'one',
    enabled: false,
    backend: 'dashscope',
    credential_source: 'DASHSCOPE_API_KEY',
    model: '',
  });
});

it('keeps a failed change visible and lets the user reload a conflict', async () => {
  vi.mocked(toolsApi.save).mockRejectedValue(
    new ApiError('TOOLS_CONFLICT', '配置已更新，请重新打开工具后保存。', 409),
  );
  render(<ToolsPage />);
  await userEvent.click(await screen.findByRole('switch', { name: '浏览器开关' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('配置已更新');
  expect(screen.getByRole('switch', { name: '浏览器开关' })).toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: '重新加载' }));
  expect(toolsApi.catalog).toHaveBeenCalledTimes(2);
});

it('filters tools and restores focus when dismissing details with Escape', async () => {
  render(<ToolsPage />);
  const button = await screen.findByRole('button', { name: /终端 执行命令/ });
  await userEvent.click(button);
  expect(await screen.findByRole('dialog', { name: '终端' })).toBeVisible();
  await userEvent.keyboard('{Escape}');
  expect(button).toHaveFocus();
  await userEvent.type(screen.getByRole('textbox', { name: '搜索工具' }), '浏览器');
  expect(screen.queryByRole('switch', { name: '终端开关' })).not.toBeInTheDocument();
  expect(screen.getByRole('switch', { name: '浏览器开关' })).toBeVisible();
});

it('can turn off a tool without entering a missing credential', async () => {
  render(<ToolsPage />);
  await userEvent.click(await screen.findByRole('switch', { name: '网页搜索开关' }));
  const dialog = await screen.findByRole('dialog', { name: '网页搜索' });
  await userEvent.click(within(dialog).getByRole('switch', { name: '启用工具' }));
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(toolsApi.save).toHaveBeenCalledWith('web_search', { revision: 'one', enabled: false });
});

it('reloads the catalog after a conflict in the configuration dialog', async () => {
  vi.mocked(toolsApi.save).mockRejectedValue(new ApiError('TOOLS_CONFLICT', '配置已更新', 409));
  render(<ToolsPage />);
  await userEvent.click(await screen.findByRole('button', { name: /浏览器 访问网页/ }));
  const dialog = await screen.findByRole('dialog', { name: '浏览器' });
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('配置已更新');
  await userEvent.click(within(dialog).getByRole('button', { name: '重新加载' }));
  expect(toolsApi.catalog).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
