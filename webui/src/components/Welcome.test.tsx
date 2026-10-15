import { StrictMode } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { Welcome } from './Welcome';
import { WELCOME_ACTIONS } from '../welcome-content';

beforeEach(() => localStorage.clear());

it('keeps the headline and six icon shortcuts stable across typing and startup changes', () => {
  const onPrompt = vi.fn();
  const { rerender } = render(
    <StrictMode>
      <Welcome active={false} onPrompt={onPrompt} />
    </StrictMode>,
  );
  const buttons = () =>
    within(screen.getByRole('group', { name: '试试这些任务' })).getAllByRole('button');
  const titles = buttons().map((button) => button.textContent);
  expect(titles).toHaveLength(6);
  for (const button of buttons())
    expect(button.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  expect(screen.getByRole('heading', { name: '今天，一起完成什么？' })).toBeVisible();
  expect(screen.queryByText('从一个想法开始')).not.toBeInTheDocument();
  expect(localStorage.getItem('mclaw.welcome.v1')).toBeNull();
  rerender(
    <StrictMode>
      <Welcome onPrompt={onPrompt} />
    </StrictMode>,
  );
  expect(buttons().map((button) => button.textContent)).toEqual(titles);
  fireEvent.click(buttons()[0]);
  expect(onPrompt).toHaveBeenCalledExactlyOnceWith(
    WELCOME_ACTIONS.find(({ title }) => title === titles[0])!.text,
  );
  rerender(
    <StrictMode>
      <Welcome onPrompt={(text) => onPrompt(text)} />
    </StrictMode>,
  );
  expect(buttons().map((button) => button.textContent)).toEqual(titles);
});
