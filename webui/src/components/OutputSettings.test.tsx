import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { api } from '../api';
import { OutputSettings } from './OutputSettings';

vi.mock('../api', () => ({ api: { settings: vi.fn() } }));

it('preserves an invalid output path and reports the backend validation error', async () => {
  vi.mocked(api.settings).mockRejectedValue(new Error('目录不可读写'));
  render(<OutputSettings initial="/old" />);
  await userEvent.clear(screen.getByLabelText('保存目录'));
  await userEvent.type(screen.getByLabelText('保存目录'), '/unavailable');
  await userEvent.click(screen.getByRole('button', { name: '保存位置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('目录不可读写');
  expect(screen.getByLabelText('保存目录')).toHaveValue('/unavailable');
});
