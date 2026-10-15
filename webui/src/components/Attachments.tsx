import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ArrowUp,
  Download,
  File as FileIcon,
  Folder,
  LoaderCircle,
  Paperclip,
  Upload,
  X,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { filesApi } from '../api';
import type { FileBrowse, FilePreview, FileReference } from '../types';
import { usePickerPosition } from './usePickerPosition';

const MAX_IMAGE_ATTACHMENTS = 8;
const imageLimitMessage = `每条消息最多添加 ${MAX_IMAGE_ATTACHMENTS} 张图片。`;

export const isImageAttachment = (file: { name: string; mime_type?: string; type?: string }) =>
  file.mime_type !== undefined
    ? file.mime_type.startsWith('image/')
    : !!file.type?.startsWith('image/') ||
      /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic|heif|tiff?|ico)$/i.test(file.name);

const sizeLabel = (bytes: number) =>
  bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 * 1024
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

export function AttachmentChips({
  files,
  onPreview,
  onRemove,
}: {
  files: (FileReference | { id: string; name: string })[];
  onPreview: (file: FileReference | { id: string; name: string }) => void;
  onRemove?: (id: string) => void;
}) {
  return (
    <div className="attachment-chips">
      {files.map((file) => (
        <span className="attachment-chip" key={file.id}>
          <button
            type="button"
            onClick={() => onPreview(file)}
            title={'path' in file ? file.path : file.name}
          >
            <FileIcon size={14} />
            <span>{file.name}</span>
            {'source' in file && file.source === 'remote' && <small>来自 {file.device}</small>}
            {'size' in file && <small>{sizeLabel(file.size)}</small>}
          </button>
          {onRemove && (
            <button
              type="button"
              className="remove-attachment"
              aria-label={`移除附件 ${file.name}`}
              onClick={() => onRemove(file.id)}
            >
              <X size={12} />
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

export function AttachmentPicker({
  ensureSession,
  onAdded,
  count,
  imageCount = 0,
  disabled,
  labelled = false,
}: {
  ensureSession: () => Promise<string>;
  onAdded: (session: string, file: FileReference) => void;
  count: number;
  imageCount?: number;
  disabled: boolean;
  labelled?: boolean;
}) {
  const [session, setSession] = useState<string | null>(null);
  const [listing, setListing] = useState<FileBrowse | null>(null);
  const [path, setPath] = useState('');
  const [tab, setTab] = useState<'upload' | 'local'>('upload');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [remaining, setRemaining] = useState(20 - count);
  const [remainingImages, setRemainingImages] = useState(MAX_IMAGE_ATTACHMENTS - imageCount);
  const picker = useRef<HTMLInputElement>(null);
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const restoreFocus = useRef(false);
  const position = usePickerPosition(!!session, anchor, { width: 420, maxHeight: 490 });
  const close = () => {
    restoreFocus.current = true;
    setSession(null);
  };
  useEffect(() => {
    if (!session && !busy && restoreFocus.current) {
      restoreFocus.current = false;
      anchor.current?.focus({ preventScroll: true });
    }
  }, [session, busy]);
  useEffect(() => {
    if (!session) return;
    const initial =
      panel.current?.querySelector<HTMLElement>('.upload-zone:not(:disabled)') ??
      panel.current?.querySelector<HTMLElement>('button:not(:disabled)');
    initial?.focus({ preventScroll: true });
  }, [session]);
  useEffect(() => {
    if (!session) return;
    const outside = (event: PointerEvent) => {
      if (
        !busy &&
        !anchor.current?.contains(event.target as Node) &&
        !panel.current?.contains(event.target as Node)
      )
        setSession(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.isComposing && !busy) {
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape);
    };
  }, [session, busy]);
  useEffect(() => setRemaining(20 - count), [count]);
  useEffect(() => setRemainingImages(MAX_IMAGE_ATTACHMENTS - imageCount), [imageCount]);
  const browse = async (target: string, location?: string) => {
    setBusy(true);
    setError('');
    try {
      const next = await filesApi.browse(target, location);
      setListing(next);
      setPath(next.path);
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取目录失败');
    } finally {
      setBusy(false);
    }
  };
  const upload = async (files: FileList | null) => {
    if (!session || !files?.length) return;
    setBusy(true);
    setError('');
    let available = remaining;
    let availableImages = remainingImages;
    const errors: string[] = [];
    for (const file of Array.from(files)) {
      if (available <= 0) {
        errors.push('每条消息最多添加 20 个附件。');
        break;
      }
      if (file.size > 20 * 1024 * 1024) {
        errors.push(`${file.name} 超过 20 MB。`);
        continue;
      }
      if (isImageAttachment(file) && availableImages <= 0) {
        errors.push(`${file.name}：${imageLimitMessage}`);
        continue;
      }
      try {
        const uploaded = await filesApi.upload(session, file);
        if (isImageAttachment(uploaded) && availableImages <= 0) {
          errors.push(`${file.name}：${imageLimitMessage}`);
          continue;
        }
        onAdded(session, uploaded);
        available -= 1;
        if (isImageAttachment(uploaded)) availableImages -= 1;
      } catch (e) {
        errors.push(`${file.name}：${e instanceof Error ? e.message : '上传失败'}`);
      }
    }
    setRemaining(available);
    setRemainingImages(availableImages);
    setBusy(false);
    setError(errors.join('\n'));
    if (!errors.length) close();
    if (picker.current) picker.current.value = '';
  };
  return (
    <>
      <button
        ref={anchor}
        type="button"
        className={`attachment-picker-trigger ${labelled ? 'workspace-button' : 'icon-button'}`}
        aria-label="添加附件"
        title="添加附件"
        aria-expanded={!!session}
        data-selected={count > 0}
        disabled={disabled || busy}
        onClick={async () => {
          setBusy(true);
          setError('');
          try {
            setSession(await ensureSession());
            setListing(null);
            setTab('upload');
          } catch (e) {
            setError(e instanceof Error ? e.message : '无法添加附件');
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy && !session ? <LoaderCircle size={18} className="spin" /> : <Paperclip size={18} />}
        {labelled && (
          <span className="attachment-trigger-label">附件{count ? ` ${count}` : ''}</span>
        )}
      </button>
      {error && !session && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      {session &&
        createPortal(
          <section
            ref={panel}
            className="composer-picker attachment-picker-popover"
            style={position}
            role="dialog"
            aria-labelledby="attachment-title"
          >
            <header className="picker-heading">
              <div>
                <strong id="attachment-title">添加附件</strong>
                <small>
                  还可添加 {remaining} 个，图片最多{' '}
                  {Math.max(0, Math.min(remaining, remainingImages))} 张
                </small>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭附件选择"
                disabled={busy}
                onClick={close}
              >
                <X size={18} />
              </button>
            </header>
            <nav className="attachment-tabs" aria-label="附件来源">
              <button
                type="button"
                className={tab === 'upload' ? 'active' : ''}
                aria-pressed={tab === 'upload'}
                disabled={busy}
                onClick={() => setTab('upload')}
              >
                <Upload size={15} />
                上传文件
              </button>
              <button
                type="button"
                className={tab === 'local' ? 'active' : ''}
                aria-pressed={tab === 'local'}
                disabled={busy}
                onClick={() => {
                  setTab('local');
                  if (!listing) void browse(session);
                }}
              >
                <Folder size={15} />
                引用本机文件
              </button>
            </nav>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            {tab === 'upload' ? (
              <>
                <input
                  ref={picker}
                  type="file"
                  multiple
                  disabled={busy || remaining <= 0}
                  className="sr-only"
                  aria-label="选择上传文件"
                  onChange={(event) => void upload(event.target.files)}
                />
                <button
                  type="button"
                  className="upload-zone"
                  disabled={busy || remaining <= 0}
                  onClick={() => picker.current?.click()}
                >
                  <span className="upload-zone-icon">
                    <Upload size={24} />
                  </span>
                  <strong>{busy ? '正在上传…' : '选择文件'}</strong>
                  <small>单个文件最大 20 MB</small>
                </button>
                <p className="attachment-storage-note">文件会保存到本次会话，可随时移除引用。</p>
              </>
            ) : (
              <>
                <p className="muted">直接引用本机文件，原文件会留在原位置。</p>
                <div className="directory-location">
                  <button
                    type="button"
                    className="icon-button"
                    aria-label="上一级目录"
                    disabled={busy || !listing?.parent}
                    onClick={() => void browse(session, listing!.parent)}
                  >
                    <ArrowUp size={17} />
                  </button>
                  <input
                    aria-label="本机文件路径"
                    value={path}
                    onChange={(event) => setPath(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault();
                        void browse(session, path);
                      }
                    }}
                  />
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => void browse(session, path)}
                  >
                    前往
                  </button>
                </div>
                <div className="file-browser-list">
                  {listing?.entries.map((entry) => (
                    <button
                      type="button"
                      key={entry.path}
                      disabled={busy || (!entry.directory && remaining <= 0)}
                      onClick={async () => {
                        if (entry.directory) {
                          await browse(session, entry.path);
                          return;
                        }
                        if (isImageAttachment(entry) && remainingImages <= 0) {
                          setError(imageLimitMessage);
                          return;
                        }
                        setBusy(true);
                        setError('');
                        try {
                          const linked = await filesApi.link(session, entry.path);
                          if (isImageAttachment(linked) && remainingImages <= 0) {
                            setError(imageLimitMessage);
                            return;
                          }
                          onAdded(session, linked);
                          setRemaining((value) => value - 1);
                          if (isImageAttachment(linked)) setRemainingImages((value) => value - 1);
                          close();
                        } catch (e) {
                          setError(e instanceof Error ? e.message : '引用文件失败');
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      {entry.directory ? <Folder size={17} /> : <FileIcon size={17} />}
                      <span>{entry.name}</span>
                      <small>{entry.directory ? '文件夹' : sizeLabel(entry.size)}</small>
                    </button>
                  ))}
                </div>
                {listing?.truncated && (
                  <p className="muted">当前目录文件较多，请进入子目录查看。</p>
                )}
                {listing && !listing.entries.length && <p className="list-empty">此目录为空。</p>}
              </>
            )}
          </section>,
          document.body,
        )}
    </>
  );
}

export function FilePreviewPanel({
  session,
  file,
  onClose,
}: {
  session: string;
  file: { id: string; name: string };
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const modal = dialog.current;
    modal?.showModal();
    close.current?.focus({ preventScroll: true });
    return () => {
      modal?.close();
      if (previous instanceof HTMLElement && previous.isConnected) {
        previous.focus({ preventScroll: true });
      }
    };
  }, []);
  useEffect(() => {
    let active = true;
    let objectUrl = '';
    setPreview(null);
    setUrl('');
    setError('');
    void filesApi
      .preview(session, file.id)
      .then(async (result) => {
        if (!active) return;
        setPreview(result);
        if (['image', 'pdf'].includes(result.kind)) {
          const blob = await filesApi.content(session, file.id);
          if (!active) return;
          objectUrl = URL.createObjectURL(new Blob([blob], { type: result.file.mime_type }));
          setUrl(objectUrl);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [session, file.id]);
  return (
    <dialog
      ref={dialog}
      className="drawer-backdrop file-preview-backdrop"
      aria-label={`预览 ${file.name}`}
      aria-modal="true"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          if (!event.nativeEvent.isComposing) onClose();
        }
        if (event.key !== 'Tab') return;
        const nodes = Array.from(
          event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), a[href], summary, iframe, [tabindex="0"]',
          ),
        );
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
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside className="detail-drawer file-preview">
        <header className="drawer-heading">
          <h2>{preview?.file.name ?? file.name}</h2>
          <button
            ref={close}
            type="button"
            className="icon-button"
            aria-label="关闭文件预览"
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>
        {error && (
          <div className="error-banner" role="alert">
            {error}
          </div>
        )}
        {!preview && !error && <p className="list-empty">正在读取文件…</p>}
        {preview && (
          <>
            <div className="file-preview-meta">
              <span>
                {sizeLabel(preview.file.size)} ·{' '}
                {preview.file.source === 'remote'
                  ? `来自 ${preview.file.device}`
                  : preview.file.source === 'upload'
                    ? '会话附件'
                    : '本机文件'}
              </span>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    const blob = await filesApi.content(session, file.id);
                    const href = URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = href;
                    link.download = preview.file.name;
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(href), 1000);
                  } catch (e) {
                    setError(e instanceof Error ? e.message : '下载失败');
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Download size={14} />
                下载
              </button>
            </div>
            <details className="plain-details">
              <summary>文件位置</summary>
              <dl className="compact-definition">
                <dt>路径</dt>
                <dd>{preview.file.path}</dd>
                <dt>设备</dt>
                <dd>{preview.file.device}</dd>
                <dt>工作区</dt>
                <dd>{preview.file.workspace}</dd>
              </dl>
            </details>
            {preview.kind === 'markdown' ? (
              <div className="markdown">
                <Markdown remarkPlugins={[remarkGfm]}>{preview.content ?? ''}</Markdown>
              </div>
            ) : preview.kind === 'text' ? (
              <pre className="file-content">{preview.content}</pre>
            ) : preview.kind === 'image' ? (
              url && <img className="preview-image" src={url} alt={preview.file.name} />
            ) : preview.kind === 'pdf' ? (
              url && (
                <iframe title={preview.file.name} className="preview-pdf" src={url} sandbox="" />
              )
            ) : (
              <p className="list-empty">这个文件适合下载后查看。</p>
            )}
            {preview.truncated && (
              <p className="notice">这里只显示部分内容，可下载完整文件查看。</p>
            )}
          </>
        )}
      </aside>
    </dialog>
  );
}
