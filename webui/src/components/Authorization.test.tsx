import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { Authorization } from './Authorization';

it('keeps a new secret in its dedicated form if the authorization request fails', async () => {
  const reply = vi.fn().mockRejectedValue(new Error('Connection failed'));
  render(
    <Authorization
      request={{
        id: 'r3',
        kind: 'secret',
        title: '凭据授权',
        needs: [{ env_var: 'SEARCH_KEY', state: 'refresh' }],
      }}
      busy={false}
      onReply={reply}
    />,
  );
  await userEvent.type(screen.getByLabelText('SEARCH_KEY'), 'test-only-retry');
  await userEvent.click(screen.getByRole('button', { name: '授权并继续' }));
  expect(screen.getByLabelText('SEARCH_KEY')).toHaveValue('test-only-retry');
});

it('submits explicit workspace trust as a boolean', async () => {
  const reply = vi.fn().mockResolvedValue(undefined);
  render(
    <Authorization
      request={{ id: 'r1', kind: 'workspace', title: '信任此工作目录', workspace: '/test/project' }}
      busy={false}
      onReply={reply}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: '信任并继续' }));
  expect(reply).toHaveBeenCalledWith(true);
});

it('uses a dedicated password field and authorizes only the selected credential scope', async () => {
  const reply = vi.fn().mockResolvedValue(undefined);
  render(
    <Authorization
      request={{
        id: 'r2',
        kind: 'secret',
        title: '凭据授权',
        required_for: 'tool:search',
        needs: [
          { env_var: 'SEARCH_KEY', provider: '搜索服务', has_value: false },
          { env_var: 'OTHER_KEY', provider: '其他服务', has_value: true },
        ],
      }}
      busy={false}
      onReply={reply}
    />,
  );
  const field = screen.getByLabelText('SEARCH_KEY');
  expect(field).toHaveAttribute('type', 'password');
  await userEvent.type(field, 'test-only-fake-value');
  await userEvent.click(screen.getByRole('button', { name: '授权并继续' }));
  expect(reply).toHaveBeenCalledWith({
    values: { SEARCH_KEY: 'test-only-fake-value' },
    authorized: ['SEARCH_KEY'],
    skipped: ['OTHER_KEY'],
  });
});
