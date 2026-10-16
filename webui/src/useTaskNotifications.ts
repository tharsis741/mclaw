import { useCallback, useEffect, useRef, useState } from 'react';
import { taskNotificationsApi } from './api';
import type { Session, TaskNotificationFeed } from './types';

const EMPTY: TaskNotificationFeed = { notifications: [], pending_count: 0, unread_session_ids: [] };
const READ_RECEIPTS = 'mclaw.task-notification-reads.v1';
const isForeground = () => document.visibilityState === 'visible' && document.hasFocus();

function loadReadReceipts(): Set<string> {
  try {
    const values: unknown = JSON.parse(sessionStorage.getItem(READ_RECEIPTS) ?? '[]');
    return new Set(
      Array.isArray(values)
        ? values.filter((id) => typeof id === 'string' && id.length > 0 && id.length <= 128)
        : [],
    );
  } catch {
    return new Set();
  }
}

function saveReadReceipts(values: Set<string>) {
  try {
    if (values.size) sessionStorage.setItem(READ_RECEIPTS, JSON.stringify([...values]));
    else sessionStorage.removeItem(READ_RECEIPTS);
  } catch {
    // Retry in memory if this browser has disabled session storage.
  }
}

export function useTaskNotifications(enabled: boolean, viewedSessionId: string | null = null) {
  const [rawFeed, setFeed] = useState<TaskNotificationFeed>(EMPTY);
  const [foreground, setForeground] = useState(isForeground);
  const [pendingReads, setPendingReads] = useState(loadReadReceipts);
  const pending = useRef(pendingReads);
  const confirmedReads = useRef(new Set<string>());
  const reading = useRef(false);
  const viewed = useRef(viewedSessionId);
  viewed.current = viewedSessionId;
  const active = useRef(enabled);
  const generation = useRef(0);
  const polling = useRef(false);
  const rememberRead = useCallback((ids: string[]) => {
    if (!active.current) return;
    const next = new Set(pending.current);
    ids.forEach((id) => {
      if (!confirmedReads.current.has(id)) next.add(id);
    });
    if (next.size === pending.current.size) return;
    pending.current = next;
    saveReadReceipts(next);
    setPendingReads(next);
  }, []);
  const observeSession = useCallback(
    (session: Session) => {
      if (session.id !== viewed.current || !isForeground()) return;
      rememberRead(
        session.messages.flatMap((message) =>
          message.task_notification_id ? [message.task_notification_id] : [],
        ),
      );
    },
    [rememberRead],
  );
  const refresh = useCallback(async () => {
    if (!active.current || polling.current) return;
    polling.current = true;
    const started = generation.current;
    try {
      const next = await taskNotificationsApi.list();
      if (active.current && generation.current === started) {
        if (viewed.current && isForeground()) {
          rememberRead(
            next.notifications
              .filter((notice) => notice.session_id === viewed.current)
              .map((notice) => notice.id),
          );
        }
        setFeed(next);
      }
    } catch {
      // The durable feed will be retried after reconnecting; never acknowledge on failure.
    } finally {
      polling.current = false;
    }
  }, [rememberRead]);
  const acknowledge = useCallback(
    async (ids: string[], action: 'dismiss' | 'read') => {
      if (!ids.length) return;
      const started = ++generation.current;
      try {
        let next = EMPTY;
        for (let offset = 0; offset < ids.length; offset += 100) {
          const batch = ids.slice(offset, offset + 100);
          next = await taskNotificationsApi.acknowledge(batch, action);
          if (action === 'read') {
            const remaining = new Set(pending.current);
            batch.forEach((id) => {
              confirmedReads.current.add(id);
              remaining.delete(id);
            });
            if (remaining.size !== pending.current.size) {
              pending.current = remaining;
              saveReadReceipts(remaining);
              if (active.current) setPendingReads(remaining);
            }
          }
        }
        if (active.current && generation.current === started) setFeed(next);
      } finally {
        generation.current += 1;
        void refresh();
      }
    },
    [refresh],
  );
  const flushReads = useCallback(async () => {
    if (!active.current || reading.current || !pending.current.size) return;
    reading.current = true;
    try {
      await acknowledge([...pending.current], 'read');
    } catch {
      // Keep observed completions suppressed across navigation and reload;
      // only a successful server acknowledgement removes a pending receipt.
    } finally {
      reading.current = false;
    }
  }, [acknowledge]);
  useEffect(() => {
    active.current = enabled;
    if (!enabled) return;
    const update = () => {
      setForeground(isForeground());
      void refresh();
      void flushReads();
    };
    update();
    const timer = setInterval(update, 1500);
    window.addEventListener('focus', update);
    window.addEventListener('blur', update);
    window.addEventListener('online', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      active.current = false;
      generation.current += 1;
      clearInterval(timer);
      window.removeEventListener('focus', update);
      window.removeEventListener('blur', update);
      window.removeEventListener('online', update);
      document.removeEventListener('visibilitychange', update);
    };
  }, [enabled, refresh, flushReads]);
  useEffect(() => {
    if (!foreground || !viewedSessionId) return;
    rememberRead(
      rawFeed.notifications
        .filter((notice) => notice.session_id === viewedSessionId)
        .map((notice) => notice.id),
    );
  }, [rawFeed, viewedSessionId, foreground, rememberRead]);
  useEffect(() => {
    void flushReads();
  }, [pendingReads, enabled, flushReads]);

  // Filter during render as well as acknowledging, so a current-session
  // completion cannot flash while its acknowledgement is still in flight.
  const notifications = rawFeed.notifications.filter(
    (notice) =>
      !pendingReads.has(notice.id) &&
      !confirmedReads.current.has(notice.id) &&
      !(foreground && notice.session_id === viewedSessionId),
  );
  const feed = {
    ...rawFeed,
    notifications,
    pending_count: Math.max(
      0,
      rawFeed.pending_count - rawFeed.notifications.length + notifications.length,
    ),
  };
  return { feed, acknowledge, refresh, observeSession, foreground };
}
