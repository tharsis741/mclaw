import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { skillsApi } from '../api';
import { Composer } from './Composer';
import type { SkillSummary } from '../types';

vi.mock('../api', () => ({ api: {}, skillsApi: { list: vi.fn() } }));

const onSend = vi.fn();
const onModel = vi.fn();
const onHelp = vi.fn();
const onSkillsChange = vi.fn();
function Editor({
  contextKey = 'one',
  disabled = false,
}: {
  contextKey?: string;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState('');
  const [skills, setSkills] = useState<string[]>([]);
  return (
    <>
      <Composer
        session={null}
        draftKey={contextKey}
        draft={draft}
        onDraft={setDraft}
        onSend={onSend}
        onStop={vi.fn()}
        onQueue={vi.fn()}
        onWorkspace={vi.fn()}
        workspace=""
        models={[
          { id: 'provider/first', label: '第一个模型' },
          { id: 'provider/second', label: '第二个模型' },
        ]}
        model="provider/first"
        onModel={onModel}
        busy={false}
        disabled={disabled}
        commands={[{ name: 'help', description: '帮助与快捷键', run: onHelp }]}
        selectedSkills={skills}
        onSkillsChange={(names) => {
          onSkillsChange(names);
          setSkills(names);
        }}
      />
      <button>其他区域</button>
    </>
  );
}
const input = () => screen.getByRole('textbox', { name: '描述你的任务' });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(skillsApi.list).mockResolvedValue({
    skills: [
      { name: 'weather', description: '查询天气', enabled: true },
      { name: 'weather-weekly', description: '每周天气', enabled: true },
      { name: 'disabled', description: '查询停用技能', enabled: false },
    ],
  });
});

it('filters skills by description and selects by arrows and Tab without sending a message', async () => {
  render(<Editor />);
  await userEvent.type(input(), '/天气');
  expect(await screen.findByRole('option', { name: /\/weather 查询天气/ })).toBeVisible();
  expect(screen.queryByRole('option', { name: /disabled/ })).not.toBeInTheDocument();
  await userEvent.keyboard('{ArrowDown}{Tab}');
  expect(onSkillsChange).toHaveBeenLastCalledWith(['weather-weekly']);
  expect(input()).toHaveValue('');
  expect(input()).toHaveFocus();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(onSend).not.toHaveBeenCalled();
});

it('selects an exact skill once and preserves that selection when choosing it again', async () => {
  render(<Editor />);
  for (let i = 0; i < 2; i++) {
    await userEvent.type(input(), '/weather');
    await screen.findByRole('option', { name: /\/weather 查询天气/ });
    await userEvent.keyboard('{Enter}');
    expect(onSkillsChange).toHaveBeenLastCalledWith(['weather']);
  }
  expect(screen.getAllByRole('button', { name: '移除技能 weather' })).toHaveLength(1);
  await userEvent.type(input(), '明天去北京');
  await userEvent.keyboard('{Enter}');
  expect(onSend).toHaveBeenCalledExactlyOnceWith('queue');
});

it('runs shortcuts by click and by the send button instead of posting their text', async () => {
  render(<Editor />);
  await userEvent.type(input(), '/help');
  await userEvent.click(screen.getByRole('option', { name: '/help 帮助与快捷键' }));
  expect(onHelp).toHaveBeenCalledOnce();
  await userEvent.type(input(), '/help');
  await userEvent.click(screen.getByRole('button', { name: '发送' }));
  expect(onHelp).toHaveBeenCalledTimes(2);
  expect(onSend).not.toHaveBeenCalled();
  expect(input()).toHaveValue('');
});

it('opens and filters models, including provider-prefixed IDs, before applying a selection', async () => {
  render(<Editor />);
  await userEvent.type(input(), '/model');
  await userEvent.keyboard('{Enter}');
  expect(screen.getByRole('listbox', { name: '选择模型' })).toBeVisible();
  expect(input()).toHaveValue('/model ');
  expect(onModel).not.toHaveBeenCalled();
  await userEvent.type(input(), 'provider/second');
  await userEvent.keyboard('{Enter}');
  expect(onModel).toHaveBeenCalledExactlyOnceWith('provider/second');
  expect(input()).toHaveValue('');
  expect(onSend).not.toHaveBeenCalled();
});

it('dismisses with Escape or an outside click and reopens when returning to the input', async () => {
  render(<Editor />);
  await userEvent.type(input(), '/');
  await screen.findByRole('option', { name: /\/weather 查询天气/ });
  await userEvent.keyboard('{Escape}');
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(input()).toHaveValue('/');
  await userEvent.type(input(), 'wea');
  expect(screen.getByRole('listbox')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '其他区域' }));
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  await userEvent.click(input());
  expect(screen.getByRole('listbox')).toBeVisible();
});

it.each(['/usr/local/bin', 'C:\\work\\project', '解释 /help 的用法', 'https://example.com/'])(
  'leaves ordinary text and paths alone: %s',
  async (text) => {
    render(<Editor />);
    await userEvent.type(input(), text);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    await userEvent.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledExactlyOnceWith('queue');
    expect(input()).toHaveValue(text);
  },
);

it('does not select or send during IME composition or on Shift+Enter', async () => {
  render(<Editor />);
  await userEvent.type(input(), '/help');
  fireEvent.compositionStart(input());
  fireEvent.keyDown(input(), { key: 'Enter', isComposing: true, keyCode: 229 });
  fireEvent.keyDown(input(), { key: 'Enter', isComposing: false });
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  fireEvent.compositionEnd(input());
  await userEvent.keyboard('{Shift>}{Enter}{/Shift}');
  expect(input()).toHaveValue('/help\n');
  expect(onHelp).not.toHaveBeenCalled();
  expect(onSend).not.toHaveBeenCalled();
});

it('keeps an unmatched query for editing and permits literal text after Escape', async () => {
  render(<Editor />);
  await userEvent.type(input(), '/unknown');
  await screen.findByText('没有匹配的操作或技能');
  await userEvent.keyboard('{Enter}');
  expect(onSend).not.toHaveBeenCalled();
  expect(input()).toHaveValue('/unknown');
  await userEvent.keyboard('{Escape}{Enter}');
  expect(onSend).toHaveBeenCalledOnce();
});

it('discards a late skill response when switching sessions and refreshes on the next open', async () => {
  let resolve!: (value: { skills: SkillSummary[] }) => void;
  vi.mocked(skillsApi.list).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const view = render(<Editor />);
  await userEvent.type(input(), '/');
  expect(screen.getByRole('status')).toHaveTextContent('正在读取技能');
  view.rerender(<Editor contextKey="two" />);
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  await act(async () => {
    resolve({ skills: [{ name: 'stale', description: '旧技能', enabled: true }] });
  });
  await userEvent.type(input(), 'weather');
  await screen.findByRole('option', { name: /\/weather 查询天气/ });
  expect(screen.queryByRole('option', { name: /stale/ })).not.toBeInTheDocument();
});

it('keeps commands usable if the skills request fails and retries on reopening', async () => {
  vi.mocked(skillsApi.list).mockRejectedValueOnce(new Error('offline'));
  render(<Editor />);
  await userEvent.type(input(), '/');
  expect(await screen.findByRole('status')).toHaveTextContent('技能暂时无法加载');
  await userEvent.click(screen.getByRole('option', { name: /\/help/ }));
  expect(onHelp).toHaveBeenCalledOnce();
  await userEvent.type(input(), '/weather');
  await screen.findByRole('option', { name: /\/weather 查询天气/ });
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
});

it('does not activate shortcuts or send when the composer is disabled', async () => {
  render(<Editor disabled />);
  await userEvent.type(input(), '/help{Enter}');
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(onHelp).not.toHaveBeenCalled();
  expect(onSend).not.toHaveBeenCalled();
});
