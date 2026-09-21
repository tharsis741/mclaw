import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { api } from '../api';
import { ServiceControls } from './ServiceControls';

vi.mock('../api', () => ({
  api: { stopService: vi.fn(), health: vi.fn() },
  ApiError: class extends Error {
    status = 0;
  },
}));

it('shows the task shutdown effect before submitting the service stop request', async () => {
  vi.mocked(api.stopService).mockResolvedValue({ status: 'stopping' });
  const requested = vi.fn();
  render(<ServiceControls onRequested={requested} />);
  await userEvent.click(screen.getByRole('button', { name: '退出 M-Claw' }));
  expect(screen.getByText(/当前任务会停止/)).toBeVisible();
  expect(api.stopService).not.toHaveBeenCalled();
  await userEvent.click(
    within(screen.getByRole('dialog')).getByRole('button', { name: '退出 M-Claw' }),
  );
  expect(await screen.findByRole('heading', { name: '正在退出 M-Claw' })).toBeVisible();
  expect(api.stopService).toHaveBeenCalledOnce();
  expect(requested).toHaveBeenCalledOnce();
});
