import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { request } from '../api';
import { ChannelsPage } from './ChannelsPage';

vi.mock('../api', () => ({ request: vi.fn() }));

it('reuses existing credentials and starts the channel only on an explicit action', async () => {
  const item = {
    id: 'dingtalk',
    label: '钉钉',
    enabled: false,
    configured: true,
    status: 'stopped',
    error: '',
    settings: {},
    credentials: { client_id: true, client_secret: true },
    fields: [
      { name: 'allow_from', label: '允许的会话', kind: 'list', options: [], advanced: false },
    ],
    validation_errors: [],
  };
  vi.mocked(request).mockImplementation(async (path, method) => {
    if (path === '/channels') return { items: [item] };
    if (path === '/channels/dingtalk' && method === 'PATCH') return item;
    if (path === '/channels/dingtalk/connect') {
      item.status = 'running';
      return item;
    }
    throw new Error('unexpected endpoint');
  });
  render(<ChannelsPage />);
  await userEvent.click(await screen.findByRole('button', { name: '设置' }));
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByLabelText('Client Secret · 已保存')).toHaveValue('');
  expect(within(dialog).getByLabelText('允许的会话')).toHaveValue('');
  await userEvent.click(within(dialog).getByRole('button', { name: '保存设置' }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith('/channels/dingtalk', 'PATCH', {
      settings: { allow_from: [] },
      credentials: {},
    }),
  );
  expect(vi.mocked(request).mock.calls.some(([path]) => path.endsWith('/connect'))).toBe(false);
  await userEvent.click(within(dialog).getByRole('button', { name: '关闭渠道设置' }));
  await userEvent.click(screen.getByRole('button', { name: '连接' }));
  expect(await screen.findByRole('button', { name: '断开' })).toBeVisible();
  expect(request).toHaveBeenCalledWith('/channels/dingtalk/connect', 'POST');
});

it('opens configuration when connecting a channel without credentials', async () => {
  vi.clearAllMocks();
  vi.mocked(request).mockResolvedValue({
    items: [
      {
        id: 'weixin',
        label: '微信',
        configured: false,
        enabled: false,
        status: 'stopped',
        error: '',
        settings: {},
        credentials: {},
        fields: [],
        validation_errors: [],
      },
    ],
  });
  render(<ChannelsPage />);
  await userEvent.click(await screen.findByRole('button', { name: '连接' }));
  expect(screen.getByRole('dialog', { name: '微信渠道设置' })).toBeVisible();
  expect(request).toHaveBeenCalledOnce();
});
