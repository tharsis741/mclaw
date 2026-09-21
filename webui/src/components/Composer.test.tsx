import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Composer } from './Composer';
import type { Session } from '../types';

const makeSession = (status: Session['status']): Session => ({
  id: 's1',
  title: '任务',
  status,
  messages: [],
  queue: [],
  active_task: { id: 'task1' },
});
const props = {
  session: null,
  draft: '任务内容',
  onDraft: vi.fn(),
  onSend: vi.fn(),
  onStop: vi.fn(),
  onModel: vi.fn(),
  onQueue: vi.fn(),
  onWorkspace: vi.fn(),
  workspace: '',
  models: [{ id: 'm1', label: '模型' }],
  model: 'm1',
  busy: false,
  disabled: false,
};
describe('composer interaction', () => {
  it('searches the model menu without sending or replacing the draft', async () => {
    const onModel = vi.fn();
    const onSend = vi.fn();
    render(
      <Composer
        {...props}
        onModel={onModel}
        onSend={onSend}
        models={[
          { id: 'm1', label: '默认模型' },
          { id: 'm2', label: '第二个模型' },
        ]}
      />,
    );
    await userEvent.click(screen.getByRole('combobox', { name: '会话模型' }));
    await userEvent.type(screen.getByRole('searchbox', { name: '搜索会话模型' }), '第二');
    await userEvent.keyboard('{Enter}');
    expect(onModel).toHaveBeenCalledWith('m2');
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('任务内容');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('keeps the draft when skills change and removes only the chosen skill', async () => {
    const onSkillsChange = vi.fn();
    const onSend = vi.fn();
    const view = render(<Composer {...props} onSend={onSend} onSkillsChange={onSkillsChange} />);
    const draft = screen.getByRole('textbox', { name: '描述你的任务' });
    view.rerender(
      <Composer
        {...props}
        onSend={onSend}
        onSkillsChange={onSkillsChange}
        selectedSkills={['weather', 'weekly']}
      />,
    );
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toBe(draft);
    expect(draft).toHaveValue('任务内容');
    await userEvent.click(screen.getByRole('button', { name: '移除技能 weather' }));
    expect(onSkillsChange).toHaveBeenCalledWith(['weekly']);
    expect(onSend).not.toHaveBeenCalled();
    view.rerender(<Composer {...props} selectedSkills={[]} />);
    expect(draft).toHaveValue('任务内容');
    expect(screen.queryByRole('button', { name: /移除技能/ })).not.toBeInTheDocument();
  });
  it('labels an automatic workspace without exposing its generated directory name', () => {
    const session = {
      ...makeSession('idle'),
      workspace: 'C:/output/session_generated',
      workspace_explicit: false,
    };
    const view = render(<Composer {...props} session={session} workspace={session.workspace} />);
    expect(screen.getByRole('button', { name: '任务文件夹' })).toHaveAttribute(
      'title',
      session.workspace,
    );
    expect(screen.queryByText('session_generated')).not.toBeInTheDocument();
    view.rerender(
      <Composer
        {...props}
        session={{ ...session, workspace_explicit: true, workspace: 'C:/work/project' }}
        workspace="C:/work/project"
      />,
    );
    expect(screen.getByRole('button', { name: 'project' })).toBeVisible();
  });
  it('opens the workspace picker without sending the draft and locks it after a task starts', async () => {
    const onWorkspace = vi.fn();
    const onSend = vi.fn();
    const view = render(<Composer {...props} onWorkspace={onWorkspace} onSend={onSend} />);
    await userEvent.click(screen.getByRole('button', { name: '选择工作区' }));
    expect(onWorkspace).toHaveBeenCalledOnce();
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox', { name: '描述你的任务' })).toHaveValue('任务内容');
    view.rerender(
      <Composer {...props} session={makeSession('running')} onWorkspace={onWorkspace} />,
    );
    expect(screen.getByRole('button', { name: '选择工作区' })).toBeDisabled();
  });
  it('offers distinct queue, steer and stop actions while running', async () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    render(
      <Composer {...props} session={makeSession('running')} onSend={onSend} onStop={onStop} />,
    );
    await userEvent.click(screen.getByRole('button', { name: '排队发送' }));
    expect(onSend).toHaveBeenLastCalledWith('queue');
    await userEvent.click(screen.getByRole('button', { name: '补充当前任务' }));
    expect(onSend).toHaveBeenLastCalledWith('steer');
    await userEvent.click(screen.getByRole('button', { name: '停止当前任务并暂停队列' }));
    expect(onStop).toHaveBeenCalledOnce();
  });
  it('permits blank resume and blocks sends until stop cleanup completes', () => {
    const { rerender } = render(
      <Composer {...props} draft="" session={makeSession('interrupted')} />,
    );
    expect(screen.getByRole('button', { name: '继续处理' })).toBeEnabled();
    rerender(<Composer {...props} session={makeSession('stopping')} />);
    expect(screen.getByRole('button', { name: '正在停止' })).toBeDisabled();
  });
  it('saves edited queue items and order only when explicitly applied', async () => {
    const onQueue = vi.fn().mockResolvedValue(undefined);
    render(
      <Composer
        {...props}
        onQueue={onQueue}
        session={{
          ...makeSession('running'),
          queue: [
            { id: 'q1', content: '第一项' },
            { id: 'q2', content: '第二项' },
          ],
        }}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /2 条排队消息/ }));
    await userEvent.click(screen.getByRole('button', { name: '上移排队消息 2' }));
    expect(onQueue).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '保存队列' }));
    expect(onQueue.mock.calls[0][0].map((item: { id: string }) => item.id)).toEqual(['q2', 'q1']);
  });
  it('does not carry queue edits to another session', async () => {
    const first = { ...makeSession('running'), queue: [{ id: 'qa', content: '原任务' }] };
    const view = render(<Composer {...props} session={first} />);
    await userEvent.click(screen.getByRole('button', { name: /1 条排队消息/ }));
    await userEvent.type(screen.getByRole('textbox', { name: '排队消息 1' }), '本地编辑');
    view.rerender(
      <Composer
        {...props}
        session={{ ...first, id: 'other', queue: [{ id: 'qb', content: '另一会话任务' }] }}
      />,
    );
    expect(screen.queryByRole('button', { name: '保存队列' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /1 条排队消息/ }));
    expect(screen.getByRole('textbox', { name: '排队消息 1' })).toHaveValue('另一会话任务');
  });
});
