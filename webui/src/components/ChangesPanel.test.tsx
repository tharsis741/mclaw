import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { ApiError, request } from '../api';
import { ChangesPanel } from './ChangesPanel';
import type { Session } from '../types';

vi.mock('../api', async () => ({
  ...(await vi.importActual<typeof import('../api')>('../api')),
  request: vi.fn(),
}));
const session: Session = {
  id: 's',
  title: '修改文件',
  status: 'completed',
  messages: [],
  queue: [],
};
const group = {
  id: 'g',
  turn_id: 't',
  state: 'current',
  updated_at: 1,
  operation_count: 1,
  operations: [],
  files: [
    {
      path: 'report.md',
      change: 'modified',
      current: { exists: true, kind: 'file', size: 2 },
      conflict: false,
    },
  ],
  direct_undo: true,
};
beforeEach(() => vi.clearAllMocks());

it('opens a fresh preview when a direct undo now requires confirmation', async () => {
  const onApplied = vi.fn();
  vi.mocked(request)
    .mockResolvedValueOnce({ groups: [group], checkpoints: [] })
    .mockRejectedValueOnce(new ApiError('CONFIRMATION_REQUIRED', '文件刚有修改', 409))
    .mockResolvedValueOnce({
      group_id: 'g',
      action: 'undo',
      confirmation_token: 'fresh',
      workspace: 'C:/work',
      files: group.files,
      conflicts: ['report.md 之后有修改'],
      diff: '- newer\n+ original',
      context_impact: { available: true, message_count: 2 },
      backup_policy: '保留当前文件副本。',
    })
    .mockResolvedValueOnce({
      success: true,
      session: {
        ...session,
        messages: [{ id: 'm', role: 'user', content: '已撤销', invalidated: true }],
      },
    })
    .mockResolvedValueOnce({ groups: [{ ...group, state: 'undone' }], checkpoints: [] });
  render(<ChangesPanel session={session} onApplied={onApplied} onClose={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '撤销最近变更' }));
  await screen.findByText('report.md 之后有修改');
  expect(onApplied).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '确认撤销' }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith('/sessions/s/changes/apply', 'POST', {
      group_id: 'g',
      action: 'undo',
      confirmation_token: 'fresh',
    }),
  );
  await waitFor(() =>
    expect(onApplied).toHaveBeenCalledWith(
      expect.objectContaining({ messages: [expect.objectContaining({ invalidated: true })] }),
    ),
  );
});

it('keeps changes inspectable while preventing rollback during execution', async () => {
  vi.mocked(request).mockResolvedValue({ groups: [group], checkpoints: [] });
  render(
    <ChangesPanel
      session={{ ...session, status: 'running' }}
      onApplied={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(await screen.findByRole('button', { name: '撤销最近变更' })).toBeDisabled();
  expect(screen.getByText('report.md')).toBeVisible();
  expect(request).toHaveBeenCalledOnce();
});

it('requires an explicit workspace grant before applying a historical undo', async () => {
  vi.mocked(request)
    .mockResolvedValueOnce({ groups: [{ ...group, direct_undo: false }], checkpoints: [] })
    .mockResolvedValueOnce({
      group_id: 'g',
      action: 'undo',
      workspace: 'C:/reviewed-work',
      workspace_trust_required: true,
      confirmation_token: 'reviewed',
      files: group.files,
      diff: '- changed',
      conflicts: [],
      context_impact: { available: true, message_count: 2 },
      backup_policy: '保留备份',
    })
    .mockResolvedValueOnce({ workspace_trusted: true })
    .mockResolvedValueOnce({ success: true, session })
    .mockResolvedValueOnce({ groups: [], checkpoints: [] });
  render(<ChangesPanel session={session} onApplied={vi.fn()} onClose={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '撤销' }));
  const confirm = await screen.findByRole('button', { name: '信任此目录并撤销' });
  expect(confirm).toBeDisabled();
  expect(request).toHaveBeenCalledTimes(2);
  await userEvent.click(
    screen.getByRole('checkbox', { name: '我信任此目录，并允许 M-Claw 在此目录中执行这些操作' }),
  );
  await userEvent.click(confirm);
  await waitFor(() =>
    expect(request).toHaveBeenNthCalledWith(3, '/sessions/s/changes/trust', 'POST', {
      workspace: 'C:/reviewed-work',
      confirmation_token: 'reviewed',
    }),
  );
  expect(request).toHaveBeenNthCalledWith(4, '/sessions/s/changes/apply', 'POST', {
    group_id: 'g',
    action: 'undo',
    confirmation_token: 'reviewed',
  });
});

it('shows shared storage scope and blocks cleanup when a recovery is pending', async () => {
  vi.mocked(request)
    .mockResolvedValueOnce({ groups: [], checkpoints: [] })
    .mockResolvedValueOnce({
      workspace: 'C:/work',
      shared_store_bytes: 1048576,
      workspace_snapshot_count: 3,
      pinned_count: 1,
      policy: { retention_days: 30, max_snapshots: 50, max_total_size_mb: 500 },
      affected_checkpoints: [],
      blocked_by_recovery: true,
    });
  render(<ChangesPanel session={session} onApplied={vi.fn()} onClose={vi.fn()} />);
  await userEvent.click(screen.getByText('恢复记录与存储'));
  expect(await screen.findByText('所有工作区共享占用')).toBeVisible();
  expect(screen.getByText('1.0 MB')).toBeVisible();
  expect(screen.getByRole('button', { name: '清理过期记录' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '清空工作区记录' })).toBeDisabled();
  expect(request).toHaveBeenCalledTimes(2);
});
