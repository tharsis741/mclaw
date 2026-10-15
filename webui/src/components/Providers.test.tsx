import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { Providers } from './Providers';
import { providersApi } from '../api';
import type { ProviderCatalog } from '../types';

vi.mock('../api', () => ({
  providersApi: {
    list: vi.fn(),
    save: vi.fn(),
    models: vi.fn(),
    library: vi.fn(),
    refreshLibrary: vi.fn(),
  },
}));
const catalog: ProviderCatalog = {
  revision: 'original-revision',
  configured: [
    {
      provider: 'deepseek',
      profile: 'api',
      runtime_provider: 'deepseek',
      label: 'DeepSeek',
      region: 'API',
      model: 'existing-model',
      credential_configured: true,
      is_default: true,
    },
  ],
  catalog: [
    {
      id: 'deepseek',
      label: 'DeepSeek',
      keywords: 'deepseek 深度求索',
      profiles: [
        {
          id: 'api',
          label: 'API',
          runtime_provider: 'deepseek',
          api_mode: 'chat_completions',
          base_url: 'https://api.deepseek.com',
          base_url_editable: false,
          credential_configured: true,
          key_url: '',
          model: 'existing-model',
          models: ['existing-model'],
        },
      ],
    },
    {
      id: 'qwen',
      label: 'Alibaba / Qwen',
      keywords: 'qwen 阿里 千问',
      profiles: [
        {
          id: 'api-cn',
          label: '中国区 API',
          runtime_provider: 'qwen',
          api_mode: 'chat_completions',
          base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
          base_url_editable: false,
          credential_configured: false,
          key_url: '',
          model: 'qwen-cn',
          models: ['qwen-cn'],
        },
        {
          id: 'api',
          label: '国际区 API',
          runtime_provider: 'qwen-intl',
          api_mode: 'chat_completions',
          base_url: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
          base_url_editable: false,
          credential_configured: true,
          key_url: '',
          model: 'qwen-intl',
          models: ['qwen-intl'],
        },
      ],
    },
  ],
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(providersApi.list).mockResolvedValue(catalog);
  vi.mocked(providersApi.save).mockResolvedValue(catalog);
});
const open = async () => {
  const callbacks = { onClose: vi.fn(), onUpdated: vi.fn().mockResolvedValue(undefined) };
  const rendered = render(<Providers {...callbacks} />);
  await screen.findByRole('button', { name: '新增供应商' });
  return { ...rendered, ...callbacks };
};
const chooseQwen = async () => {
  await userEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  await userEvent.click(screen.getByRole('button', { name: 'Alibaba / Qwen' }));
};

it('searches candidates locally by brand or Chinese alias, then shows one provider form', async () => {
  await open();
  await userEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  const input = screen.getByRole('textbox', { name: '搜索供应商' });
  expect(input).toHaveFocus();
  await userEvent.type(input, '千问');
  expect(screen.getByRole('button', { name: 'Alibaba / Qwen' })).toBeVisible();
  expect(screen.queryByRole('button', { name: /DeepSeek/ })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Alibaba / Qwen' }));
  expect(screen.getByLabelText('API 区域与接口')).toBeVisible();
  expect(screen.getByLabelText('API Key')).toHaveValue('');
  expect(providersApi.list).toHaveBeenCalledOnce();
  expect(providersApi.models).not.toHaveBeenCalled();
  expect(providersApi.refreshLibrary).not.toHaveBeenCalled();
});

it('refreshes the shared model library explicitly and updates the provider candidates', async () => {
  const status = {
    updated_at: 1800000000,
    providers: 30,
    models: 1000,
    refreshing: false,
    last_refresh: { ok: true, finished_at: 1800000000 },
  };
  let resolve!: (value: typeof status) => void;
  vi.mocked(providersApi.refreshLibrary).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const { onUpdated } = await open();
  await userEvent.click(screen.getByRole('button', { name: '刷新模型库' }));
  expect(screen.getByRole('button', { name: '正在刷新…' })).toBeDisabled();
  expect(providersApi.refreshLibrary).toHaveBeenCalledOnce();
  await userEvent.click(screen.getByRole('button', { name: /DeepSeek/ }));
  await userEvent.type(screen.getByLabelText('API Key'), 'fixture-unsaved-key');
  const updated = structuredClone(catalog);
  updated.library = status;
  updated.catalog[0].profiles[0].models.push('new-cached-model');
  vi.mocked(providersApi.list).mockResolvedValue(updated);
  await act(async () => resolve(status));
  expect(await screen.findByRole('status')).toHaveTextContent('模型库已更新，共 1,000 个模型');
  expect(onUpdated).toHaveBeenCalledOnce();
  expect(screen.getByLabelText('API Key')).toHaveValue('fixture-unsaved-key');
  expect(screen.getByLabelText('模型')).toHaveValue('existing-model');
  expect(screen.queryByRole('option', { name: 'new-cached-model' })).not.toBeInTheDocument();
  await userEvent.clear(screen.getByLabelText('模型'));
  expect(screen.getByRole('option', { name: 'new-cached-model' })).toBeVisible();
  expect(providersApi.save).not.toHaveBeenCalled();
});

it('keeps the configured providers and allows retry when the library refresh fails', async () => {
  vi.mocked(providersApi.refreshLibrary).mockResolvedValue({
    updated_at: null,
    providers: 0,
    models: 0,
    refreshing: false,
    last_refresh: { ok: false, finished_at: 1800000000 },
  });
  const { onUpdated } = await open();
  await userEvent.click(screen.getByRole('button', { name: '刷新模型库' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('原有列表已保留');
  expect(screen.getByRole('button', { name: /DeepSeek/ })).toBeVisible();
  expect(screen.getByRole('button', { name: '刷新模型库' })).toBeEnabled();
  expect(onUpdated).not.toHaveBeenCalled();
});

it('aborts the refresh response when the dialog closes', async () => {
  vi.mocked(providersApi.refreshLibrary).mockReturnValue(new Promise(() => {}));
  const { unmount } = await open();
  await userEvent.click(screen.getByRole('button', { name: '刷新模型库' }));
  const signal = vi.mocked(providersApi.refreshLibrary).mock.lastCall![0]!;
  unmount();
  expect(signal.aborted).toBe(true);
});

it('clears a typed key on region change and saves only the selected provider without changing the default', async () => {
  const callbacks = await open();
  await chooseQwen();
  await userEvent.type(screen.getByLabelText('API Key'), 'fixture-cn-key');
  await userEvent.click(screen.getByRole('combobox', { name: 'API 区域与接口' }));
  await userEvent.click(screen.getByRole('option', { name: '国际区 API' }));
  expect(screen.getByLabelText('API Key')).toHaveValue('');
  expect(screen.getByLabelText('API Key')).toHaveAttribute('placeholder', '已保存，留空沿用');
  expect(screen.getByLabelText('模型')).toHaveValue('qwen-intl');
  expect(screen.getByRole('checkbox', { name: '设为新会话的默认模型' })).not.toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(providersApi.save).toHaveBeenCalledWith(
    expect.objectContaining({
      provider: 'qwen',
      profile: 'api',
      api_key: '',
      model: 'qwen-intl',
      make_default: false,
      revision: 'original-revision',
    }),
  );
  expect(await screen.findByRole('heading', { name: '供应商与模型' })).toBeVisible();
  expect(callbacks.onUpdated).toHaveBeenCalledOnce();
});

it('keeps failed saves editable and permits an explicit default selection', async () => {
  vi.mocked(providersApi.save).mockRejectedValueOnce(new Error('配置已在其他窗口更新'));
  await open();
  await chooseQwen();
  await userEvent.type(screen.getByLabelText('API Key'), 'fixture-key');
  await userEvent.click(screen.getByRole('checkbox', { name: '设为新会话的默认模型' }));
  await userEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('配置已在其他窗口更新');
  expect(screen.getByLabelText('API Key')).toHaveValue('fixture-key');
  expect(providersApi.save).toHaveBeenCalledWith(expect.objectContaining({ make_default: true }));
});

it('aborts a model request on region change and ignores its late response', async () => {
  let resolve!: (value: { models: string[] }) => void;
  vi.mocked(providersApi.models).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  await open();
  await chooseQwen();
  await userEvent.type(screen.getByLabelText('API Key'), 'fixture-key');
  await userEvent.click(screen.getByRole('button', { name: '获取模型' }));
  const signal = vi.mocked(providersApi.models).mock.calls[0][1]!;
  await userEvent.click(screen.getByRole('combobox', { name: 'API 区域与接口' }));
  await userEvent.click(screen.getByRole('option', { name: '国际区 API' }));
  expect(signal.aborted).toBe(true);
  await act(async () => resolve({ models: ['wrong-region-model'] }));
  expect(screen.queryByDisplayValue('wrong-region-model')).not.toBeInTheDocument();
  expect(screen.getByLabelText('模型')).toHaveValue('qwen-intl');
  expect(screen.getByRole('button', { name: '获取模型' })).toBeEnabled();
});

it('disposes list and probe requests on every close without saving or scheduling a poll', async () => {
  for (let count = 0; count < 8; count++) {
    const rendered = await open();
    await chooseQwen();
    vi.mocked(providersApi.models).mockReturnValue(new Promise(() => {}));
    await userEvent.click(screen.getByRole('button', { name: '获取模型' }));
    const listSignal = vi.mocked(providersApi.list).mock.lastCall![0]!;
    const probeSignal = vi.mocked(providersApi.models).mock.lastCall![1]!;
    rendered.unmount();
    expect(listSignal.aborted).toBe(true);
    expect(probeSignal.aborted).toBe(true);
  }
  expect(providersApi.list).toHaveBeenCalledTimes(8);
  expect(providersApi.save).not.toHaveBeenCalled();
});

it('supports Escape and keeps keyboard focus inside the dialog', async () => {
  const { onClose } = await open();
  const dialog = screen.getByRole('dialog');
  const buttons = within(dialog).getAllByRole('button');
  buttons[buttons.length - 1].focus();
  await userEvent.tab();
  expect(buttons[0]).toHaveFocus();
  await userEvent.keyboard('{Escape}');
  expect(onClose).toHaveBeenCalledOnce();
});

it('returns to the configured list when cancelling an edit and does not offer to unset the active default', async () => {
  await open();
  await userEvent.click(screen.getByRole('button', { name: /DeepSeek/ }));
  expect(screen.getByRole('checkbox', { name: '当前默认模型' })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: '当前默认模型' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.getByRole('heading', { name: '供应商与模型' })).toBeVisible();
  expect(providersApi.save).not.toHaveBeenCalled();
});

it('handles a failed catalog request and retries without starting a background workflow', async () => {
  vi.mocked(providersApi.list).mockRejectedValueOnce(new Error('服务未连接'));
  render(<Providers onClose={vi.fn()} onUpdated={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('服务未连接');
  await userEvent.click(screen.getByRole('button', { name: '重新加载' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '新增供应商' })).toBeVisible());
  expect(providersApi.list).toHaveBeenCalledTimes(2);
});
