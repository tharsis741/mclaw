import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { skillsApi } from '../api';
import type { SkillDetail } from '../types';
import { SkillsPage } from './SkillsPage';

vi.mock('../api', () => ({
  skillsApi: { list: vi.fn(), detail: vi.fn(), remove: vi.fn() },
}));

const skill: SkillDetail = {
  name: 'weekly',
  description: '整理周报',
  enabled: true,
  source: 'local',
  version: 'version-1',
  content: '# 周报\n整理摘要',
  tree: { entries: [] },
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(skillsApi.list).mockResolvedValue({ skills: [skill] });
  vi.mocked(skillsApi.detail).mockResolvedValue(skill);
  vi.mocked(skillsApi.remove).mockResolvedValue({ success: true });
});

async function openConfirmation() {
  const view = render(<SkillsPage onUse={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: /weekly 整理周报/ }));
  const drawer = screen.getByRole('dialog', { name: 'weekly 技能详情' });
  const trigger = within(drawer).getByRole('button', { name: '删除技能' });
  await userEvent.click(trigger);
  const dialog = screen.getByRole('dialog', { name: '删除技能' });
  return { ...view, drawer, trigger, dialog };
}

it('opens a separate modal confirmation and initially focuses cancel', async () => {
  const { drawer, dialog } = await openConfirmation();
  expect(dialog).toHaveAttribute('open');
  expect(dialog).toHaveAttribute('aria-modal', 'true');
  expect(drawer).not.toContainElement(dialog);
  expect(within(dialog).getByText('确定删除「weekly」吗？')).toBeVisible();
  expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus();
  expect(skillsApi.remove).not.toHaveBeenCalled();
});

it.each(['cancel', 'close', 'escape', 'backdrop'])(
  'dismisses with %s while retaining details and restoring focus',
  async (method) => {
    const { drawer, trigger, dialog } = await openConfirmation();
    if (method === 'cancel')
      await userEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    else if (method === 'close')
      await userEvent.click(within(dialog).getByRole('button', { name: '关闭删除确认' }));
    else if (method === 'escape') await userEvent.keyboard('{Escape}');
    else await userEvent.click(dialog);
    expect(screen.queryByRole('dialog', { name: '删除技能' })).toBeNull();
    expect(drawer).toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(skillsApi.remove).not.toHaveBeenCalled();
  },
);

it('deletes the reviewed version once and refreshes the list after completion', async () => {
  let finish!: (value: { success: boolean }) => void;
  vi.mocked(skillsApi.remove).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const { dialog } = await openConfirmation();
  const confirm = within(dialog).getByRole('button', { name: '确认删除' });
  await userEvent.click(confirm);
  expect(confirm).toHaveTextContent('正在删除…');
  expect(confirm).toBeDisabled();
  expect(within(dialog).getByRole('button', { name: '取消' })).toBeDisabled();
  expect(within(dialog).getByRole('button', { name: '关闭删除确认' })).toBeDisabled();
  await userEvent.click(confirm);
  await userEvent.keyboard('{Escape}');
  fireEvent(dialog, new Event('cancel', { cancelable: true }));
  await userEvent.click(dialog);
  expect(dialog).toBeInTheDocument();
  expect(skillsApi.remove).toHaveBeenCalledExactlyOnceWith('weekly', 'version-1');
  vi.mocked(skillsApi.list).mockResolvedValue({ skills: [] });
  await act(async () => finish({ success: true }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(screen.queryByRole('button', { name: /weekly 整理周报/ })).toBeNull();
  expect(screen.getByText('0 个技能')).toBeVisible();
});

it('keeps deletion errors in the confirmation and allows retry', async () => {
  vi.mocked(skillsApi.remove).mockRejectedValueOnce(new Error('技能已变化，请重新读取'));
  const { dialog } = await openConfirmation();
  await userEvent.click(within(dialog).getByRole('button', { name: '确认删除' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('技能已变化，请重新读取');
  expect(within(dialog).getByRole('button', { name: '确认删除' })).toBeEnabled();
  expect(within(dialog).getByRole('button', { name: '取消' })).toBeEnabled();
  vi.mocked(skillsApi.list).mockResolvedValue({ skills: [] });
  await userEvent.click(within(dialog).getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(skillsApi.remove).toHaveBeenCalledTimes(2);
  expect(skillsApi.remove).toHaveBeenLastCalledWith('weekly', 'version-1');
});
