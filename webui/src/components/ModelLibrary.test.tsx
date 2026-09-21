import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { providersApi } from '../api';
import type { ModelLibrarySnapshot } from '../types';
import { ModelLibrary } from './ModelLibrary';

vi.mock('../api', () => ({ providersApi: { library: vi.fn() } }));
const snapshot: ModelLibrarySnapshot = {
  status: {
    updated_at: 1800000000,
    providers: 1,
    models: 101,
    refreshing: false,
    last_refresh: null,
  },
  providers: [
    {
      id: 'deepseek',
      label: 'DeepSeek',
      profiles: [
        {
          provider: 'deepseek',
          label: 'DeepSeek',
          profile: 'api',
          region: 'DeepSeek API',
          callable: true,
        },
      ],
    },
  ],
  models: [
    {
      id: 'model-one',
      name: 'Model One',
      provider: 'deepseek',
      provider_label: 'DeepSeek',
      context: 128000,
    },
  ],
  total: 101,
  offset: 0,
  limit: 100,
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(providersApi.library).mockResolvedValue(snapshot);
});

it('shows cache metadata, filters by provider and searches model names', async () => {
  render(<ModelLibrary />);
  expect(await screen.findByText('Model One')).toBeVisible();
  expect(screen.getByText('上下文 128,000 tokens')).toBeVisible();
  await userEvent.click(screen.getByRole('combobox', { name: '筛选模型库供应商' }));
  await userEvent.click(screen.getByRole('option', { name: 'DeepSeek' }));
  expect(await screen.findByText('DeepSeek API')).toBeVisible();
  await userEvent.type(screen.getByRole('textbox', { name: '搜索模型库' }), 'model-one');
  await waitFor(() =>
    expect(providersApi.library).toHaveBeenLastCalledWith(
      'model-one',
      'deepseek',
      0,
      expect.any(AbortSignal),
    ),
  );
});

it('paginates model results and releases pending reads when unmounted', async () => {
  const { unmount } = render(<ModelLibrary />);
  await screen.findByText('Model One');
  vi.mocked(providersApi.library).mockReturnValue(new Promise(() => {}));
  await userEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() =>
    expect(providersApi.library).toHaveBeenLastCalledWith('', '', 100, expect.any(AbortSignal)),
  );
  const signal = vi.mocked(providersApi.library).mock.lastCall![3]!;
  unmount();
  expect(signal.aborted).toBe(true);
});

it('reloads after a cache refresh and distinguishes an empty cache from no search matches', async () => {
  vi.mocked(providersApi.library).mockResolvedValue({
    ...snapshot,
    status: { ...snapshot.status, models: 0 },
    models: [],
    total: 0,
  });
  const { rerender } = render(<ModelLibrary updatedAt={null} />);
  expect(await screen.findByText('还没有模型库，点击上方“刷新模型库”获取。')).toBeVisible();
  vi.mocked(providersApi.library).mockResolvedValue(snapshot);
  rerender(<ModelLibrary updatedAt={1800000000} />);
  expect(await screen.findByText('Model One')).toBeVisible();
  vi.mocked(providersApi.library).mockResolvedValue({ ...snapshot, models: [], total: 0 });
  await userEvent.type(screen.getByRole('textbox', { name: '搜索模型库' }), 'missing');
  expect(await screen.findByText('没有找到匹配的供应商或模型。')).toBeVisible();
});

it('ignores a late response from a previous search', async () => {
  let resolve!: (value: ModelLibrarySnapshot) => void;
  vi.mocked(providersApi.library).mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  render(<ModelLibrary />);
  await waitFor(() => expect(providersApi.library).toHaveBeenCalledOnce());
  const oldSignal = vi.mocked(providersApi.library).mock.calls[0][3]!;
  await userEvent.type(screen.getByRole('textbox', { name: '搜索模型库' }), 'current');
  await screen.findByText('Model One');
  expect(oldSignal.aborted).toBe(true);
  await act(async () =>
    resolve({ ...snapshot, models: [{ ...snapshot.models[0], name: 'Stale model' }] }),
  );
  expect(screen.queryByText('Stale model')).not.toBeInTheDocument();
});
