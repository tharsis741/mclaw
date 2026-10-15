import { useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { skillsApi } from '../api';
import { SkillsPicker } from './SkillsPicker';

vi.mock('../api', () => ({ skillsApi: { list: vi.fn() } }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(skillsApi.list).mockResolvedValue({
    skills: [{ name: 'weather', description: '查询天气', enabled: true }],
  });
});

function Picker() {
  const [selected, setSelected] = useState(['disabled-selected']);
  return <SkillsPicker selected={selected} onChange={setSelected} />;
}

it('selects long labels by keyboard and allows removing a previously selected disabled skill', async () => {
  vi.mocked(skillsApi.list).mockResolvedValue({
    skills: [
      {
        name: 'long-unbroken-skill-name',
        description: 'LongDescriptionWithoutSpaces'.repeat(8),
        enabled: true,
      },
      { name: 'disabled-selected', description: '旧选择', enabled: false },
      { name: 'disabled-new', description: '已停用技能', enabled: false },
    ],
  });
  render(<Picker />);
  await userEvent.click(screen.getByRole('button', { name: '技能 1' }));
  const dialog = screen.getByRole('dialog', { name: '选择技能' });
  const checkbox = await within(dialog).findByRole('checkbox', {
    name: /long-unbroken-skill-name/,
  });
  await userEvent.click(checkbox);
  expect(checkbox).toBeChecked();
  await userEvent.keyboard(' ');
  expect(checkbox).not.toBeChecked();
  expect(within(dialog).getByRole('checkbox', { name: /disabled-new/ })).toBeDisabled();
  const previous = within(dialog).getByRole('checkbox', { name: /disabled-selected/ });
  expect(previous).toBeEnabled();
  await userEvent.click(previous);
  expect(previous).not.toBeChecked();
  expect(previous).toBeDisabled();
  await userEvent.click(within(dialog).getByRole('button', { name: '关闭技能选择' }));
  expect(screen.getByRole('button', { name: '技能' })).toHaveAttribute('aria-expanded', 'false');
});

it('keeps internal interactions open and dismisses on an outside pointer without losing selections', async () => {
  render(
    <>
      <Picker />
      <button>其他区域</button>
    </>,
  );
  await userEvent.click(screen.getByRole('button', { name: '技能 1' }));
  await userEvent.type(screen.getByRole('textbox', { name: '查找技能' }), 'weather');
  await userEvent.click(await screen.findByRole('checkbox', { name: /weather/ }));
  expect(screen.getByRole('dialog', { name: '选择技能' })).toBeVisible();
  const outside = screen.getByRole('button', { name: '其他区域' });
  await userEvent.click(outside);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(outside).toHaveFocus();
  await userEvent.click(screen.getByRole('button', { name: '技能 2' }));
  expect(await screen.findByRole('checkbox', { name: /weather/ })).toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: '技能 2' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('closes on Escape and returns focus to the trigger across repeated opens', async () => {
  render(<Picker />);
  const trigger = screen.getByRole('button', { name: '技能 1' });
  for (let i = 0; i < 2; i++) {
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('textbox', { name: '查找技能' }));
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  }
});

it('locks an open picker while busy and clears choices only when enabled', async () => {
  const onChange = vi.fn();
  const view = render(<SkillsPicker selected={['weather']} onChange={onChange} />);
  await userEvent.click(screen.getByRole('button', { name: '技能 1' }));
  await screen.findByRole('checkbox', { name: /weather/ });
  view.rerender(<SkillsPicker selected={['weather']} onChange={onChange} disabled />);
  expect(screen.getByRole('checkbox', { name: /weather/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: '清空' })).toBeDisabled();
  view.rerender(<SkillsPicker selected={['weather']} onChange={onChange} />);
  await userEvent.click(screen.getByRole('button', { name: '清空' }));
  expect(onChange).toHaveBeenCalledWith([]);
});
