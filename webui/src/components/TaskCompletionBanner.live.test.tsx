import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { TaskCompletionBanner } from './TaskCompletionBanner';
import { useTaskNotifications } from '../useTaskNotifications';
import { taskNotificationsApi } from '../api';
import type { Session, TaskNotificationFeed } from '../types';

vi.mock('../api', async () => ({
  ...(await vi.importActual<typeof import('../api')>('../api')),
  taskNotificationsApi: { list: vi.fn(), acknowledge: vi.fn() },
}));
const directory = process.env.MCLAW_TASK_LIVE_REPORT_DIR;
const liveTest = directory ? it : it.skip;
const read = (name: string) => JSON.parse(readFileSync(resolve(directory!, name), 'utf8'));
const feed = (): TaskNotificationFeed => read('task-notifications-live.json').feed;
beforeEach(() => {
  sessionStorage.clear();
  vi.mocked(taskNotificationsApi.list).mockReset();
  vi.mocked(taskNotificationsApi.acknowledge).mockReset();
});
afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

liveTest(
  'suppresses only the viewed session and preserves other completions while its read is pending',
  async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const data = feed();
    const current = read('task-notifications-live.json').sessions[0] as Session;
    const ids = data.notifications
      .filter((notice) => notice.session_id === current.id)
      .map((notice) => notice.id);
    const other = data.notifications.filter((notice) => notice.session_id !== current.id);
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(data);
    let saved!: (value: TaskNotificationFeed) => void;
    vi.mocked(taskNotificationsApi.acknowledge).mockImplementation(
      () =>
        new Promise((resolve) => {
          saved = resolve;
        }),
    );
    const view = renderHook(
      ({ selected }: { selected: string | null }) => useTaskNotifications(true, selected),
      { initialProps: { selected: current.id as string | null } },
    );
    await waitFor(() => expect(taskNotificationsApi.acknowledge).toHaveBeenCalledWith(ids, 'read'));
    expect(view.result.current.feed.notifications).toEqual(other);
    expect(view.result.current.feed.pending_count).toBe(other.length);
    view.rerender({ selected: null });
    expect(view.result.current.feed.notifications).toEqual(other);
    const remaining = {
      notifications: other,
      pending_count: other.length,
      unread_session_ids: other.map((notice) => notice.session_id),
    };
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(remaining);
    await act(async () => saved(remaining));
    expect(view.result.current.feed.notifications).toEqual(other);
  },
);

liveTest(
  'retains observed streamed completions across navigation and reload when the read request fails',
  async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const current = read('task-notifications-live.json').sessions[0] as Session;
    const ids = current.messages.flatMap((message) =>
      message.task_notification_id ? [message.task_notification_id] : [],
    );
    const empty = read('task-notifications-restored.json').after_read as TaskNotificationFeed;
    const unseen = {
      notifications: feed().notifications.filter((notice) => notice.session_id === current.id),
      pending_count: ids.length,
      unread_session_ids: [current.id],
    };
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(empty);
    vi.mocked(taskNotificationsApi.acknowledge).mockRejectedValue(new Error('offline'));
    const view = renderHook(
      ({ selected }: { selected: string | null }) => useTaskNotifications(true, selected),
      { initialProps: { selected: current.id as string | null } },
    );
    await act(async () => view.result.current.observeSession(current));
    await waitFor(() => expect(taskNotificationsApi.acknowledge).toHaveBeenCalledWith(ids, 'read'));
    view.rerender({ selected: null });
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(unseen);
    await act(async () => view.result.current.refresh());
    expect(view.result.current.feed.pending_count).toBe(0);
    expect(JSON.parse(sessionStorage.getItem('mclaw.task-notification-reads.v1')!)).toEqual(ids);
    view.unmount();
    let saved!: (value: TaskNotificationFeed) => void;
    vi.mocked(taskNotificationsApi.acknowledge).mockImplementation(
      () =>
        new Promise((resolve) => {
          saved = resolve;
        }),
    );
    const restored = renderHook(() => useTaskNotifications(true));
    await waitFor(() => expect(saved).toBeDefined());
    expect(restored.result.current.feed.notifications).toEqual([]);
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(empty);
    await act(async () => saved(empty));
    expect(sessionStorage.getItem('mclaw.task-notification-reads.v1')).toBeNull();
    expect(restored.result.current.feed.pending_count).toBe(0);
  },
);

liveTest(
  'does not mark an unfocused conversation read and waits until it is actually viewed',
  async () => {
    const focus = vi.spyOn(document, 'hasFocus').mockReturnValue(false);
    const data = feed();
    const current = read('task-notifications-live.json').sessions[0] as Session;
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(data);
    const other = data.notifications.filter((notice) => notice.session_id !== current.id);
    vi.mocked(taskNotificationsApi.acknowledge).mockResolvedValue({
      notifications: other,
      pending_count: other.length,
      unread_session_ids: other.map((notice) => notice.session_id),
    });
    const view = renderHook(
      ({ selected }: { selected: string | null }) => useTaskNotifications(true, selected),
      { initialProps: { selected: current.id as string | null } },
    );
    await act(async () => view.result.current.observeSession(current));
    await waitFor(() => expect(view.result.current.feed.pending_count).toBe(3));
    expect(taskNotificationsApi.acknowledge).not.toHaveBeenCalled();
    view.rerender({ selected: null });
    focus.mockReturnValue(true);
    fireEvent.focus(window);
    expect(view.result.current.feed.pending_count).toBe(3);
    expect(taskNotificationsApi.acknowledge).not.toHaveBeenCalled();
    view.rerender({ selected: current.id });
    await waitFor(() =>
      expect(taskNotificationsApi.acknowledge).toHaveBeenCalledWith(
        expect.arrayContaining(
          current.messages.flatMap((message) =>
            message.task_notification_id ? [message.task_notification_id] : [],
          ),
        ),
        'read',
      ),
    );
    expect(view.result.current.feed.notifications).toEqual(other);
  },
);

liveTest('shows the real final-reply preview without stealing the draft focus', async () => {
  vi.spyOn(document, 'hasFocus').mockReturnValue(true);
  const data = feed();
  const viewResult = vi.fn().mockResolvedValue(undefined);
  const dismiss = vi.fn().mockResolvedValue(undefined);
  const view = render(
    <>
      <input aria-label="草稿" />
      <TaskCompletionBanner count={0} suspended={false} onView={viewResult} onDismiss={dismiss} />
    </>,
  );
  screen.getByRole('textbox').focus();
  view.rerender(
    <>
      <input aria-label="草稿" />
      <TaskCompletionBanner
        notice={data.notifications[0]}
        count={data.pending_count}
        suspended={false}
        onView={viewResult}
        onDismiss={dismiss}
      />
    </>,
  );
  expect(screen.getByRole('textbox')).toHaveFocus();
  expect(screen.getAllByRole('region', { name: '任务完成通知' })).toHaveLength(1);
  expect(screen.getByText(data.notifications[0].preview)).toBeVisible();
  expect(screen.getByText('+2')).toBeVisible();
  expect(screen.queryByRole('dialog')).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: '查看结果' }));
  expect(viewResult).toHaveBeenCalledWith(data.notifications[0]);
  await userEvent.click(screen.getByRole('button', { name: '关闭任务完成通知' }));
  expect(dismiss).toHaveBeenCalledWith(data.notifications[0]);
});

liveTest(
  'waits behind dialogs and in unfocused windows without acknowledging the completion',
  () => {
    const focus = vi.spyOn(document, 'hasFocus').mockReturnValue(false);
    const props = {
      notice: feed().notifications[0],
      count: 1,
      suspended: false,
      onView: vi.fn(),
      onDismiss: vi.fn(),
    };
    const view = render(<TaskCompletionBanner {...props} />);
    expect(screen.queryByRole('region')).toBeNull();
    focus.mockReturnValue(true);
    fireEvent.focus(window);
    expect(screen.getByRole('region')).toBeVisible();
    view.rerender(<TaskCompletionBanner {...props} suspended />);
    expect(screen.queryByRole('region')).toBeNull();
    expect(props.onDismiss).not.toHaveBeenCalled();
    view.rerender(<TaskCompletionBanner {...props} />);
    expect(screen.getByRole('region')).toBeVisible();
  },
);

liveTest('does not resurrect a dismissed completion from a stale poll response', async () => {
  const data = feed();
  const cleared = read('task-notifications-restored.json').after_read as TaskNotificationFeed;
  vi.mocked(taskNotificationsApi.list).mockResolvedValue(data);
  vi.mocked(taskNotificationsApi.acknowledge).mockResolvedValue(cleared);
  const { result } = renderHook(() => useTaskNotifications(true));
  await waitFor(() => expect(result.current.feed.pending_count).toBe(3));
  let resolveOld!: (value: TaskNotificationFeed) => void;
  vi.mocked(taskNotificationsApi.list).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
  );
  act(() => {
    void result.current.refresh();
  });
  await act(() =>
    result.current.acknowledge(
      data.notifications.map((item) => item.id),
      'read',
    ),
  );
  expect(result.current.feed.pending_count).toBe(0);
  await act(async () => resolveOld(data));
  expect(result.current.feed.pending_count).toBe(0);
});

liveTest(
  'keeps pending completions when acknowledgement fails and reloads durable state',
  async () => {
    const data = feed();
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(data);
    vi.mocked(taskNotificationsApi.acknowledge).mockRejectedValueOnce(new Error('offline'));
    const view = renderHook(() => useTaskNotifications(true));
    await waitFor(() => expect(view.result.current.feed.pending_count).toBe(3));
    await act(async () => {
      await expect(
        view.result.current.acknowledge([data.notifications[0].id], 'dismiss'),
      ).rejects.toThrow('offline');
    });
    expect(view.result.current.feed.pending_count).toBe(3);
    view.unmount();
    vi.mocked(taskNotificationsApi.list).mockResolvedValue(
      read('task-notifications-live.json').after_dismiss,
    );
    const restored = renderHook(() => useTaskNotifications(true));
    await waitFor(() => expect(restored.result.current.feed.pending_count).toBe(2));
    expect(restored.result.current.feed.unread_session_ids).toContain(
      data.notifications[1].session_id,
    );
  },
);

liveTest('recovers missed completions after a network interruption', async () => {
  vi.mocked(taskNotificationsApi.list)
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue(feed());
  const { result } = renderHook(() => useTaskNotifications(true));
  await act(async () => {});
  expect(result.current.feed.pending_count).toBe(0);
  fireEvent.online(window);
  await waitFor(() => expect(result.current.feed.pending_count).toBe(3));
});
