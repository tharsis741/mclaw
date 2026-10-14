import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { Conversation } from './Conversation';
import type { Message, Session, SkillReviewNotice } from '../types';

// Consume records from real-provider runs; no model responses are synthesized.
const directory = process.env.MCLAW_LIVE_REPORT_DIR;
const liveTest = directory ? it : it.skip;
const read = (name: string) => JSON.parse(readFileSync(resolve(directory!, name), 'utf8'));

liveTest('shows a durable collapsed notice and opens the actual saved Skill', async () => {
  const session = read('ui-notification-session.json') as Session;
  const notice = session.messages.find((message) => message.skill_review)!.skill_review!;
  const open = vi.fn();
  const view = render(<Conversation session={session} onOpenSkill={open} />);
  const details = view.container.querySelector('.skill-review-notice details')!;
  expect(details).not.toHaveAttribute('open');
  await userEvent.click(screen.getByText('技能已保存', { exact: true }));
  expect(details).toHaveAttribute('open');
  expect(screen.getByText(notice.skills[0].name)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '查看技能' }));
  expect(open).toHaveBeenCalledWith(notice.skills[0].name);
  view.unmount();
  // A freshly loaded snapshot still contains the notification after remount.
  const restored = render(<Conversation session={read('ui-notification-session.json')} />);
  expect(restored.container.querySelectorAll('.skill-review-notice')).toHaveLength(1);
  expect(restored.container.querySelector('dialog')).toBeNull();
  expect(restored.container.querySelector('.skill-review-notice details')).not.toHaveAttribute(
    'open',
  );
});

liveTest(
  'preserves expansion across real notice revisions and resets on a different conversation',
  () => {
    const session = read('ui-notification-session.json') as Session;
    const records = read('worker-notification.json');
    const saved = session.messages.find((message) => message.skill_review)!;
    const running: Message = { ...saved, skill_review: records.events[0].event.notice };
    const view = render(<Conversation session={{ ...session, messages: [running] }} />);
    fireEvent.click(view.container.querySelector('.skill-review-summary')!);
    expect(view.container.querySelector('details')).toHaveAttribute('open');
    view.rerender(<Conversation session={{ ...session, messages: [saved] }} />);
    expect(view.container.querySelector('details')).toHaveAttribute('open');
    expect(view.container.querySelectorAll('.skill-review-notice')).toHaveLength(1);
    view.rerender(
      <Conversation session={{ ...session, id: 'other-conversation', messages: [] }} />,
    );
    expect(view.container.querySelector('.skill-review-notice')).toBeNull();
  },
);

liveTest('retains the actual saved Skill after a later failed Agent request', async () => {
  const record = read('controlled-partial.json');
  const session: Session = {
    id: 'partial',
    title: '',
    status: 'idle',
    queue: [],
    messages: [record.notification],
  };
  const view = render(<Conversation session={session} />);
  await userEvent.click(screen.getByText('技能已保存，后续整理未完成'));
  expect(screen.getByText(record.notification.skill_review.skills[0].name)).toBeVisible();
  expect(view.container.querySelector('.skill-review-error')).toBeVisible();
  expect(view.container.querySelector('[role="dialog"]')).toBeNull();
});

liveTest('shows the actual saved actions as one updated Skill', async () => {
  const notice = read('updated-notice.json') as SkillReviewNotice;
  const session: Session = {
    id: notice.session_id,
    title: '',
    status: 'idle',
    queue: [],
    messages: [{ id: notice.id, role: 'system', content: '', skill_review: notice }],
  };
  const view = render(<Conversation session={session} />);
  expect(screen.getByText('更新 1 项')).toBeVisible();
  await userEvent.click(screen.getByText('技能已保存', { exact: true }));
  expect(view.container.querySelectorAll('.skill-review-item')).toHaveLength(1);
  const actions = notice.skills[0].actions;
  expect(actions.some((action) => ['edit', 'patch', 'evolution_update'].includes(action))).toBe(true);
  for (const [action, label] of [
    ['edit', '更新技能正文'],
    ['patch', '修改技能正文'],
    ['evolution_update', '更新经验'],
  ]) {
    const rendered = view.container.querySelector('.skill-review-actions');
    if (actions.includes(action)) expect(rendered).toHaveTextContent(label);
    else expect(rendered).not.toHaveTextContent(label);
  }
});

liveTest('does not claim a save when the real review failed before writing', async () => {
  const notice = read('failed-notice.json');
  const session: Session = {
    id: notice.session_id,
    title: '',
    status: 'idle',
    queue: [],
    messages: [{ id: notice.id, role: 'system', content: '', skill_review: notice }],
  };
  const view = render(<Conversation session={session} />);
  expect(screen.queryByText('技能已保存', { exact: true })).toBeNull();
  await userEvent.click(screen.getByText('技能整理未完成'));
  expect(view.container.querySelector('.skill-review-error')).toBeVisible();
  expect(view.container.querySelector('.skill-review-item')).toBeNull();
});
