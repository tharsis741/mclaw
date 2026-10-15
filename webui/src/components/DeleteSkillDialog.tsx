import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Trash2, X } from 'lucide-react';

export function DeleteSkillDialog({
  name,
  onClose,
  onDelete,
}: {
  name: string;
  onClose: () => void;
  onDelete: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const deleting = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const previous = document.activeElement;
    const modal = dialog.current;
    modal?.showModal();
    cancel.current?.focus({ preventScroll: true });
    return () => {
      modal?.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus({ preventScroll: true });
    };
  }, []);

  const remove = async () => {
    if (deleting.current) return;
    deleting.current = true;
    setBusy(true);
    setError('');
    try {
      await onDelete();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败，请重试。');
      deleting.current = false;
      setBusy(false);
    }
  };

  return createPortal(
    <dialog
      ref={dialog}
      className="modal-backdrop skill-delete-backdrop"
      aria-modal="true"
      aria-labelledby="skill-delete-title"
      aria-describedby="skill-delete-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          if (!busy) onClose();
        }
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section className="modal skill-delete-dialog">
        <header>
          <div className="skill-delete-heading">
            <Trash2 size={20} aria-hidden="true" />
            <h2 id="skill-delete-title">删除技能</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭删除确认"
            disabled={busy}
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>
        <p className="skill-delete-name">确定删除「{name}」吗？</p>
        <p id="skill-delete-description">将删除该技能及其文件，已发送任务中的记录会保留。</p>
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button ref={cancel} className="secondary" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button
            className="primary skill-delete-confirm"
            disabled={busy}
            onClick={() => void remove()}
          >
            {busy ? '正在删除…' : '确认删除'}
          </button>
        </div>
      </section>
    </dialog>,
    document.body,
  );
}
