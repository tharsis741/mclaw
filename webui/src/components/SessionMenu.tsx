import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { Download, MoreHorizontal, Pencil, Trash2, X } from 'lucide-react';
import type { Session } from '../types';

export function SessionMenu({
  session,
  open,
  onOpenChange,
  onRename,
  onExport,
  onDelete,
}: {
  session: Session;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRename: () => void;
  onExport: () => void;
  onDelete: () => void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<CSSProperties>({});
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const height = popup.current?.offsetHeight ?? 135;
      setPosition({
        left: Math.max(8, Math.min(rect.right - 160, window.innerWidth - 168)),
        top: Math.max(
          8,
          rect.bottom + height + 12 > window.innerHeight ? rect.top - height - 6 : rect.bottom + 6,
        ),
      });
    };
    place();
    popup.current?.querySelector('button')?.focus();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      const target = event.target as Node;
      if (!trigger.current?.contains(target) && !popup.current?.contains(target))
        onOpenChange(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      onOpenChange(false);
      trigger.current?.focus();
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('focusin', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('focusin', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open, onOpenChange]);
  const select = (action: () => void) => {
    onOpenChange(false);
    trigger.current?.focus();
    action();
  };
  return (
    <>
      <button
        ref={trigger}
        className="session-menu icon-button"
        aria-label={`${session.title} 的菜单`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => onOpenChange(!open)}
      >
        <MoreHorizontal size={15} />
      </button>
      {open &&
        createPortal(
          <div
            ref={popup}
            className="session-popover"
            style={position}
            role="menu"
            aria-label="会话操作"
            onKeyDown={(event) => {
              const items = Array.from(
                popup.current?.querySelectorAll<HTMLButtonElement>('button') ?? [],
              );
              const index = items.indexOf(document.activeElement as HTMLButtonElement);
              let next = index;
              if (event.key === 'ArrowDown') next = (index + 1) % items.length;
              else if (event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
              else if (event.key === 'Home') next = 0;
              else if (event.key === 'End') next = items.length - 1;
              else return;
              event.preventDefault();
              items[next]?.focus();
            }}
          >
            <button role="menuitem" onClick={() => select(onRename)}>
              <Pencil size={14} />
              重命名
            </button>
            <button role="menuitem" onClick={() => select(onExport)}>
              <Download size={14} />
              导出 JSON
            </button>
            <button
              role="menuitem"
              className="session-delete-action"
              onClick={() => select(onDelete)}
            >
              <Trash2 size={14} />
              删除会话
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}

export function DeleteSessionDialog({
  session,
  onClose,
  onDelete,
}: {
  session: Session;
  onClose: () => void;
  onDelete: () => Promise<void>;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.querySelector<HTMLButtonElement>('[data-cancel]')?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  const remove = async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await onDelete();
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除失败，请重试');
      setBusy(false);
    }
  };
  return (
    <div
      className="modal-backdrop"
      onClick={() => {
        if (!busy) onClose();
      }}
    >
      <div
        ref={panel}
        className="modal session-delete-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="session-delete-title"
        aria-describedby="session-delete-description"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !busy) onClose();
          if (event.key !== 'Tab') return;
          const items = Array.from(
            panel.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [],
          );
          const first = items[0],
            last = items.at(-1);
          if (!items.length) event.preventDefault();
          else if (
            event.shiftKey &&
            (document.activeElement === first || document.activeElement === panel.current)
          ) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <header>
          <h3 id="session-delete-title">删除会话</h3>
          <button className="icon-button" aria-label="关闭" disabled={busy} onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <p className="session-delete-name">{session.title || '新会话'}</p>
        <p id="session-delete-description">
          会话记录、草稿和排队任务将被删除，工作区中的文件会保留。此操作无法撤销。
        </p>
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button data-cancel disabled={busy} onClick={onClose}>
            取消
          </button>
          <button
            className="primary session-delete-confirm"
            disabled={busy}
            onClick={() => void remove()}
          >
            {busy ? '正在删除…' : '删除'}
          </button>
        </div>
      </div>
    </div>
  );
}
