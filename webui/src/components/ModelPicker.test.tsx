import { useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { ModelPicker } from './ModelPicker';

const models = [
  { id: 'deepseek::shared', label: '同名模型', provider: 'deepseek', provider_label: 'DeepSeek' },
  { id: 'deepseek::fast', label: '快速模型', provider: 'deepseek', provider_label: 'DeepSeek' },
  { id: 'custom::shared', label: '同名模型', provider: 'custom', provider_label: '公司模型服务' },
];

function Example() {
  const [value, setValue] = useState(models[0].id);
  return (
    <>
      <ModelPicker models={models} value={value} onChange={setValue} />
      <button>其他区域</button>
      <output>{value}</output>
    </>
  );
}

it('groups models by the configured provider and keeps identical model names distinct', async () => {
  render(<Example />);
  await userEvent.click(screen.getByRole('combobox', { name: '会话模型' }));
  expect(screen.getByRole('group', { name: 'DeepSeek' })).toBeVisible();
  const custom = screen.getByRole('group', { name: '公司模型服务' });
  await userEvent.click(within(custom).getByRole('option', { name: '同名模型' }));
  expect(screen.getByRole('status')).toHaveTextContent('custom::shared');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('combobox')).toHaveFocus();
});

it('filters by provider, then searches provider names across the full catalogue', async () => {
  render(<Example />);
  await userEvent.click(screen.getByRole('combobox'));
  const providers = screen.getByRole('navigation', { name: '模型供应商' });
  await userEvent.click(within(providers).getByRole('button', { name: /DeepSeek/ }));
  expect(screen.getAllByRole('option')).toHaveLength(2);
  await userEvent.type(screen.getByRole('searchbox'), '公司');
  expect(screen.getAllByRole('option')).toHaveLength(1);
  await userEvent.keyboard('{Enter}');
  expect(screen.getByRole('status')).toHaveTextContent('custom::shared');
});

it('supports keyboard navigation and closes outside or with Escape without changing the model', async () => {
  render(<Example />);
  const trigger = screen.getByRole('combobox');
  trigger.focus();
  await userEvent.keyboard('{ArrowDown}{ArrowDown}{Enter}');
  expect(screen.getByRole('status')).toHaveTextContent('deepseek::fast');
  await userEvent.click(trigger);
  await userEvent.keyboard('{ArrowDown}{Escape}');
  expect(trigger).toHaveFocus();
  expect(screen.getByRole('status')).toHaveTextContent('deepseek::fast');
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole('button', { name: '其他区域' }));
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('shows empty results without allowing Enter to pick a hidden model', async () => {
  const onChange = vi.fn();
  render(<ModelPicker models={models} value={models[0].id} onChange={onChange} />);
  await userEvent.click(screen.getByRole('combobox'));
  await userEvent.type(screen.getByRole('searchbox'), '没有这个模型');
  expect(screen.getByText('没有找到匹配的模型')).toBeVisible();
  await userEvent.keyboard('{Enter}');
  expect(onChange).not.toHaveBeenCalled();
});

it('preserves an older session model and uses the same provider group as current choices', async () => {
  render(<ModelPicker models={models} value="deepseek::old-model" onChange={vi.fn()} />);
  expect(screen.getByRole('combobox')).toHaveTextContent('old-model');
  await userEvent.click(screen.getByRole('combobox'));
  const group = screen.getByRole('group', { name: 'DeepSeek' });
  expect(within(group).getByRole('option', { name: /old-model/ })).toHaveAttribute(
    'aria-selected',
    'true',
  );
});

it('closes when disabled and opens provider management without choosing a model', async () => {
  const onManage = vi.fn();
  const onChange = vi.fn();
  const view = render(
    <ModelPicker models={models} value={models[0].id} onChange={onChange} onManage={onManage} />,
  );
  await userEvent.click(screen.getByRole('combobox'));
  await userEvent.click(screen.getByRole('button', { name: '管理供应商' }));
  expect(onManage).toHaveBeenCalledOnce();
  expect(onChange).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('combobox'));
  view.rerender(<ModelPicker models={models} value={models[0].id} onChange={onChange} disabled />);
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('combobox')).toBeDisabled();
});

it('groups regional endpoints under one supplier while preserving the chosen route', async () => {
  const onChange = vi.fn();
  render(
    <ModelPicker
      models={[
        {
          id: 'qwen::qwen3-plus',
          label: 'qwen3-plus',
          provider: 'qwen',
          provider_label: 'Alibaba / Qwen (China)',
        },
        {
          id: 'qwen-intl::qwen3-plus',
          label: 'qwen3-plus',
          provider: 'qwen-intl',
          provider_label: 'Alibaba / Qwen (International)',
        },
      ]}
      value="qwen::qwen3-plus"
      onChange={onChange}
    />,
  );
  await userEvent.click(screen.getByRole('combobox'));
  expect(screen.getByText('1 个供应商 · 2 个模型')).toBeVisible();
  expect(screen.getAllByRole('group')).toHaveLength(1);
  expect(screen.getByRole('group', { name: '通义千问' })).toBeVisible();
  await userEvent.type(screen.getByRole('searchbox'), '国际区');
  expect(screen.getAllByRole('option')).toHaveLength(1);
  await userEvent.keyboard('{Enter}');
  expect(onChange).toHaveBeenCalledWith('qwen-intl::qwen3-plus');
});
