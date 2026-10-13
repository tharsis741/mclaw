import { memo, useEffect, useId, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ArrowUpRight,
  Check,
  Circle,
  CircleAlert,
  GitBranch,
  LoaderCircle,
  Pause,
  X,
} from 'lucide-react';
import type { Delegation, FileReference, Session, SubagentTask } from '../types';
import { Conversation, TOOL_LABELS } from './Conversation';

const STATUS: Record<SubagentTask['status'], string> = {
  pending: '等待开始',
  running: '执行中',
  finalizing: '整理结果',
  completed: '已完成',
  failed: '失败',
  timed_out: '已超时',
  interrupted: '已中断',
  completion_unknown: '结果待核对',
};
const ACTIVE = new Set(['pending', 'running', 'finalizing']);

function tasksFromHistory(group: Delegation): SubagentTask[] {
  if (group.tasks) return Object.values(group.tasks).sort((a, b) => a.task_index - b.task_index);
  // Older releases saved lifecycle events and summaries, but not child tool details.
  return (group.goals ?? group.result?.results.map((result) => result.goal ?? '') ?? []).map(
    (goal, index) => {
      const result = group.result?.results.find((item) => item.task_index === index);
      const events = group.events?.filter((event) => event.task_index === index) ?? [];
      const completed = [...events]
        .reverse()
        .find((event) => event.event_type === 'completed' || event.event_type === 'error');
      const data = result ?? completed?.data;
      const status = String(
        data?.status ?? (completed?.event_type === 'error' ? 'failed' : 'completion_unknown'),
      );
      const summary = String(data?.summary ?? '');
      return {
        task_index: index,
        goal,
        legacy: true,
        status: status in STATUS ? (status as SubagentTask['status']) : 'completion_unknown',
        model: result?.model,
        duration_seconds: result?.duration_seconds,
        error: data?.error ? String(data.error) : undefined,
        messages: [
          { id: 'assignment', role: 'user', source: 'delegate', content: goal },
          { id: 'reply', role: 'assistant', content: summary },
        ],
      };
    },
  );
}

function StatusIcon({ status }: { status: SubagentTask['status'] }) {
  if (status === 'pending') return <Circle size={14} />;
  if (ACTIVE.has(status)) return <LoaderCircle size={14} className="spin" />;
  if (status === 'completed') return <Check size={14} />;
  if (status === 'interrupted') return <Pause size={14} />;
  return <CircleAlert size={14} />;
}

type FilePreview = (file: FileReference | { id: string; name: string }) => void;

const SubagentConversation = memo(
  function SubagentConversation({
    session,
    onFilePreview,
  }: {
    session: Session;
    visible: boolean;
    onFilePreview: FilePreview;
  }) {
    return <Conversation session={session} onFilePreview={onFilePreview} />;
  },
  (previous, next) => !previous.visible && !next.visible,
);

function SubagentDialog({
  identifier,
  task,
  open,
  onClose,
  onFilePreview,
}: {
  identifier: string;
  task: SubagentTask;
  open: boolean;
  onClose: () => void;
  onFilePreview: FilePreview;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const heading = useId();
  const session = useMemo<Session>(
    () => ({
      id: `subagent:${identifier}:${task.task_index}`,
      title: task.goal,
      status: ACTIVE.has(task.status) ? 'running' : 'completed',
      messages: task.messages,
      queue: [],
    }),
    [identifier, task],
  );
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    close.current?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  return createPortal(
    <div
      className="modal-backdrop subagent-backdrop"
      hidden={!open}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="modal subagent-dialog"
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={heading}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.stopPropagation();
            onClose();
          }
          if (event.key !== 'Tab') return;
          const nodes = Array.from(
            panel.current?.querySelectorAll<HTMLElement>(
              'button:not(:disabled), a[href], summary, [tabindex="0"]',
            ) ?? [],
          ).filter((node) => node.getClientRects().length > 0);
          const first = nodes[0],
            last = nodes.at(-1);
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <header className="subagent-header">
          <div className="subagent-heading">
            <span className="subagent-eyebrow">
              <GitBranch size={13} />
              子代理
            </span>
            <h3 id={heading}>{task.goal || `子任务 ${task.task_index + 1}`}</h3>
            <div className={`subagent-meta state-${task.status}`}>
              <StatusIcon status={task.status} />
              <span>{STATUS[task.status]}</span>
              {task.model && <span>{task.model}</span>}
              {task.duration_seconds != null && <span>{Math.round(task.duration_seconds)} 秒</span>}
            </div>
          </div>
          <button ref={close} className="icon-button" aria-label="关闭子代理详情" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        {task.legacy && <p className="subagent-notice">这条历史记录只保存了任务状态和结果。</p>}
        {task.error && (
          <p className="subagent-notice error" role="alert">
            {task.error}
          </p>
        )}
        <SubagentConversation
          key={session.id}
          session={session}
          visible={open}
          onFilePreview={onFilePreview}
        />
        {ACTIVE.has(task.status) && (
          <div className="subagent-live" role="status">
            <StatusIcon status={task.status} />
            {task.status_text || STATUS[task.status]}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

export function Subagents({
  identifier,
  group,
  onFilePreview,
}: {
  identifier: string;
  group: Delegation;
  onFilePreview: FilePreview;
}) {
  const tasks = useMemo(() => tasksFromHistory(group), [group]);
  const [selected, setSelected] = useState<number | null>(null);
  const [open, setOpen] = useState(false);
  const selectedTask = tasks.find((task) => task.task_index === selected);
  return (
    <div className="subagents">
      <div className="subagents-label">
        <GitBranch size={14} />
        <span>子代理</span>
        <span>{tasks.length} 项任务</span>
      </div>
      {tasks.map((task) => {
        const title = task.goal.split('\n')[0] || `子任务 ${task.task_index + 1}`;
        const reply = task.messages.at(-1);
        const lastEntry = reply?.process?.at(-1);
        const latestText = reply?.content.trim().split('\n').filter(Boolean).at(-1);
        const activity = ACTIVE.has(task.status)
          ? lastEntry?.kind === 'tool' && lastEntry.status === 'running'
            ? TOOL_LABELS[lastEntry.title] || lastEntry.title
            : latestText || task.status_text || STATUS[task.status]
          : task.error || reply?.content || STATUS[task.status];
        return (
          <button
            className={`subagent-row state-${task.status}`}
            key={task.task_index}
            aria-label={`查看子代理：${title}`}
            onClick={() => {
              setSelected(task.task_index);
              setOpen(true);
            }}
          >
            <span className="subagent-state-icon">
              <StatusIcon status={task.status} />
            </span>
            <span className="subagent-row-text">
              <span className="subagent-row-title">{title}</span>
              <span className="subagent-row-activity">
                {activity.replace(/\s+/g, ' ').slice(0, 160)}
              </span>
            </span>
            <span className="subagent-row-status">{STATUS[task.status]}</span>
            <ArrowUpRight size={14} className="subagent-open" />
          </button>
        );
      })}
      {selectedTask && (
        <SubagentDialog
          identifier={identifier}
          task={selectedTask}
          open={open}
          onClose={() => setOpen(false)}
          onFilePreview={onFilePreview}
        />
      )}
    </div>
  );
}
