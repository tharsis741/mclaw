import { useEffect, useState } from 'react';
import { ArrowUpRight, Check, X } from 'lucide-react';
import type { TaskCompletionNotice } from '../types';

export function TaskCompletionBanner({
  notice,
  count,
  suspended,
  onView,
  onDismiss,
}: {
  notice?: TaskCompletionNotice;
  count: number;
  suspended: boolean;
  onView: (notice: TaskCompletionNotice) => Promise<void>;
  onDismiss: (notice: TaskCompletionNotice) => Promise<void>;
}) {
  const [foreground, setForeground] = useState(
    () => document.visibilityState === 'visible' && document.hasFocus(),
  );
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const update = () =>
      setForeground(document.visibilityState === 'visible' && document.hasFocus());
    window.addEventListener('focus', update);
    window.addEventListener('blur', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      window.removeEventListener('focus', update);
      window.removeEventListener('blur', update);
      document.removeEventListener('visibilitychange', update);
    };
  }, []);
  if (!notice || suspended || !foreground) return null;
  const act = async (action: (item: TaskCompletionNotice) => Promise<void>) => {
    setBusy(true);
    try {
      await action(notice);
    } catch {
      /* App keeps the notification and reports the error. */
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="task-completion-banner" aria-label="任务完成通知" key={notice.id}>
      <span className="task-completion-icon">
        <Check size={15} aria-hidden="true" />
      </span>
      <div className="task-completion-copy" role="status" aria-live="polite" aria-atomic="true">
        <div className="task-completion-heading">
          <strong>任务已完成</strong>
          <span className="task-completion-title" title={notice.title}>
            {notice.title}
          </span>
          {count > 1 && (
            <span className="task-completion-count" title={`还有 ${count - 1} 项完成通知`}>
              +{count - 1}
            </span>
          )}
        </div>
        <div className="task-completion-preview" title={notice.preview}>
          {notice.preview || '结果已保存，可查看完整回复。'}
        </div>
      </div>
      <button className="task-completion-view" disabled={busy} onClick={() => void act(onView)}>
        查看结果
        <ArrowUpRight size={13} aria-hidden="true" />
      </button>
      <button
        className="task-completion-close"
        aria-label="关闭任务完成通知"
        disabled={busy}
        onClick={() => void act(onDismiss)}
      >
        <X size={16} aria-hidden="true" />
      </button>
    </section>
  );
}
