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
  disabled,
  labelled = false,
}: {
  ensureSession: () => Promise<string>;
  onAdded: (session: string, file: FileReference) => void;
  count: number;
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
  const picker = useRef<HTMLInputElement>(null);
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const position = usePickerPosition(!!session, anchor, { width: 420, maxHeight: 490 });
  const close = () => {
    setSession(null);
    anchor.current?.focus({ preventScroll: true });
  };
  useEffect(() => {
    if (!session) return;
    panel.current?.querySelector<HTMLElement>('.upload-zone')?.focus({ preventScroll: true });
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
      try {
        onAdded(session, await filesApi.upload(session, file));
        available -= 1;
      } catch (e) {
        errors.push(`${file.name}：${e instanceof Error ? e.message : '上传失败'}`);
      }
    }
    setRemaining(available);
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
        disabled={disabled || busy || count >= 20}
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
                <small>还可添加 {remaining} 个</small>
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
                        setBusy(true);
                        setError('');
                        try {
                          onAdded(session, await filesApi.link(session, entry.path));
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
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="detail-drawer file-preview"
        role="dialog"
        aria-modal="true"
        aria-label={`预览 ${file.name}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-heading">
          <h2>{preview?.file.name ?? file.name}</h2>
          <button type="button" className="icon-button" aria-label="关闭文件预览" onClick={onClose}>
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
    </div>
  );
}
