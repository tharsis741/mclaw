import { render } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { Session } from '../types';
import { Conversation } from './Conversation';

const parse = vi.hoisted(() => vi.fn());
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => {
    parse(children);
    return <p>{children}</p>;
  },
}));

it('does not parse historical messages or closed reasoning when the current response grows', () => {
  const session: Session = {
    id: 's',
    title: '任务',
    status: 'running',
    queue: [],
    messages: [
      ...Array.from({ length: 80 }, (_, index) => ({
        id: String(index),
        role: 'assistant' as const,
        content: `历史 ${index}`,
        process: [
          {
            id: `r${index}`,
            kind: 'reasoning',
            title: '思考',
            content: '折叠的长内容'.repeat(100),
          },
        ],
      })),
      { id: 'live', role: 'assistant', content: '正在生成' },
    ],
  };
  const { rerender } = render(<Conversation session={session} onFilePreview={() => {}} />);
  expect(parse.mock.calls.some(([content]) => content.includes('折叠的长内容'))).toBe(false);
  parse.mockClear();
  rerender(
    <Conversation
      session={{
        ...session,
        messages: [
          ...session.messages.slice(0, -1),
          { ...session.messages.at(-1)!, content: '正在生成回复' },
        ],
      }}
      onFilePreview={() => {}}
    />,
  );
  expect(parse.mock.calls.some(([content]) => content.startsWith('历史'))).toBe(false);
  expect(parse).toHaveBeenCalledWith('正在生成回复');
});
