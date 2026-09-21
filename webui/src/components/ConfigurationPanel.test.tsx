import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { ApiError, settingsApi } from '../api';
import type { ConfigurationField, ConfigurationSnapshot } from '../types';
import { ConfigurationPanel } from './ConfigurationPanel';

vi.mock('../api', async () => ({
  ...(await vi.importActual<typeof import('../api')>('../api')),
  settingsApi: { configuration: vi.fn(), saveConfiguration: vi.fn() },
}));
const field = (
  path: string,
  label: string,
  value: number | boolean,
  advanced = false,
): ConfigurationField => ({
  path,
  label,
  value,
  advanced,
  kind: typeof value === 'boolean' ? 'boolean' : 'number',
  description: '',
  minimum: 1,
  maximum: 10000,
  step: 1,
  options: [],
});
const snapshot: ConfigurationSnapshot = {
  revision: 'r1',
  groups: [
    {
      id: 'model',
      page: 'general',
      title: '模型行为',
      effect: '新建会话生效',
      fields: [field('prompt_cache.enabled', '提示词缓存', true)],
    },
    {
      id: 'terminal',
      page: 'execution',
      title: '命令与文件',
      effect: '下一次任务生效',
      fields: [
        field('terminal.timeout', '命令默认超时（秒）', 180),
        field('terminal.max_timeout', '命令超时上限（秒）', 600, true),
      ],
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(settingsApi.configuration).mockResolvedValue(structuredClone(snapshot));
  vi.mocked(settingsApi.saveConfiguration).mockResolvedValue({
    ...structuredClone(snapshot),
    revision: 'r2',
  });
});

it('loads shared values and saves only changed fields with the revision', async () => {
  render(<ConfigurationPanel page="execution" />);
  const timeout = await screen.findByRole('spinbutton', { name: '命令默认超时（秒）' });
  expect(timeout).toHaveValue(180);
  expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled();
  await userEvent.clear(timeout);
  await userEvent.type(timeout, '240');
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await waitFor(() =>
    expect(settingsApi.saveConfiguration).toHaveBeenCalledWith('r1', { 'terminal.timeout': 240 }),
  );
  expect(await screen.findByText('已保存')).toBeVisible();
});

it('keeps advanced settings collapsed and allows related fields to save together', async () => {
  render(<ConfigurationPanel page="execution" />);
  await screen.findByRole('spinbutton', { name: '命令默认超时（秒）' });
  expect(screen.getByRole('spinbutton', { name: '命令超时上限（秒）' })).not.toBeVisible();
  await userEvent.click(screen.getByText('高级设置'));
  const maximum = screen.getByRole('spinbutton', { name: '命令超时上限（秒）' });
  await userEvent.clear(maximum);
  await userEvent.type(maximum, '1200');
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await waitFor(() =>
    expect(settingsApi.saveConfiguration).toHaveBeenCalledWith('r1', {
      'terminal.max_timeout': 1200,
    }),
  );
});

it('retains drafts on conflict and requires an explicit reload before saving again', async () => {
  vi.mocked(settingsApi.saveConfiguration).mockRejectedValueOnce(
    new ApiError('CONFIGURATION_CONFLICT', '配置已更新', 409),
  );
  render(<ConfigurationPanel page="general" />);
  await userEvent.click(await screen.findByRole('switch', { name: '提示词缓存' }));
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('配置已更新');
  expect(screen.getByRole('switch', { name: '提示词缓存' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled();
  vi.mocked(settingsApi.configuration).mockResolvedValue({
    ...structuredClone(snapshot),
    revision: 'external',
  });
  await userEvent.click(screen.getByRole('button', { name: '读取最新配置' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存设置' })).toBeEnabled());
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await waitFor(() =>
    expect(settingsApi.saveConfiguration).toHaveBeenLastCalledWith('external', {
      'prompt_cache.enabled': false,
    }),
  );
});

it('preserves unsaved values when switching tabs and saves only the active group', async () => {
  const view = render(<ConfigurationPanel page="general" />);
  await userEvent.click(await screen.findByRole('switch', { name: '提示词缓存' }));
  view.rerender(<ConfigurationPanel page="execution" />);
  const timeout = screen.getByRole('spinbutton', { name: '命令默认超时（秒）' });
  await userEvent.clear(timeout);
  await userEvent.type(timeout, '300');
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await screen.findByText('已保存');
  expect(settingsApi.saveConfiguration).toHaveBeenCalledWith('r1', { 'terminal.timeout': 300 });
  view.rerender(<ConfigurationPanel page="general" />);
  expect(screen.getByRole('switch', { name: '提示词缓存' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: '保存设置' })).toBeEnabled();
  expect(screen.getByText('有未保存的修改')).toBeVisible();
});

it('reloads external changes when revisiting a tab with no draft', async () => {
  const view = render(<ConfigurationPanel page="general" />);
  expect(await screen.findByRole('switch', { name: '提示词缓存' })).toBeChecked();
  const updated = structuredClone(snapshot);
  updated.groups[0].fields[0].value = false;
  vi.mocked(settingsApi.configuration).mockResolvedValue(updated);
  view.rerender(<ConfigurationPanel page={null} />);
  view.rerender(<ConfigurationPanel page="general" />);
  await waitFor(() => expect(screen.getByRole('switch', { name: '提示词缓存' })).not.toBeChecked());
});

it('keeps the edited value after a validation failure', async () => {
  vi.mocked(settingsApi.saveConfiguration).mockRejectedValue(
    new ApiError('INVALID_CONFIGURATION', '默认超时不能超过上限', 422),
  );
  render(<ConfigurationPanel page="execution" />);
  const timeout = await screen.findByRole('spinbutton', { name: '命令默认超时（秒）' });
  await userEvent.clear(timeout);
  await userEvent.type(timeout, '900');
  await userEvent.click(screen.getByRole('button', { name: '保存设置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('默认超时不能超过上限');
  expect(timeout).toHaveValue(900);
});
