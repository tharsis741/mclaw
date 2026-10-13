import { ModelPicker } from './ModelPicker';
import { useEffect, useRef, useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  FolderOpen,
  ListOrdered,
  LoaderCircle,
  Send,
  Square,
  Trash2,
  X,
  BookOpen,
  Monitor,
} from 'lucide-react';
import { isPaused, isRunning, primaryAction } from '../session-state';
import type { FileReference, ModelOption, QueueItem, Session } from '../types';
import { api } from '../api';
import { SkillsPicker } from './SkillsPicker';
import { DevicesPicker } from './DevicesPicker';
import { VoiceInput } from './VoiceInput';
import { AttachmentChips, AttachmentPicker, isImageAttachment } from './Attachments';
import { useSlashCommands } from './useSlashCommands';
import type { SlashCommand } from './useSlashCommands';

export function Composer({
  session,
  draft,
  onDraft,
  onSend,
  onStop,
  onModel,
  onManageProviders,
  onQueue,
  onWorkspace,
  workspace,
  models,
  model,
  busy,
  disabled,
  selectedSkills = [],
  onSkillsChange = () => {},
  onTranscript,
  draftKey,
  ensureSession,
  attachments = [],
  onAttachmentAdded,
  onAttachmentRemove,
  onFilePreview = () => {},
  commands = [],
  onFocusChange,
  collaboration = false,
  selectedDevices = [],
  onDevicesChange = () => {},
}: {
  session: Session | null;
  draft: string;
  onDraft: (value: string) => void;
  onSend: (mode: 'queue' | 'steer') => void;
  onStop: () => void;
  onModel: (model: string) => void;
  onManageProviders?: () => void;
  onQueue: (items: QueueItem[]) => Promise<void>;
  onWorkspace: () => void;
  workspace: string;
  models: ModelOption[];
  model: string;
  busy: boolean;
  disabled: boolean;
  selectedSkills?: string[];
  onSkillsChange?: (names: string[]) => void;
  onTranscript?: (key: string, text: string) => void;
  draftKey?: string;
  ensureSession?: () => Promise<string>;
  attachments?: (FileReference | { id: string; name: string })[];
  onAttachmentAdded?: (session: string, file: FileReference) => void;
  onAttachmentRemove?: (id: string) => void;
  onFilePreview?: (file: FileReference | { id: string; name: string }) => void;
  commands?: SlashCommand[];
  onFocusChange?: (focused: boolean) => void;
  collaboration?: boolean;
  selectedDevices?: string[];
  onDevicesChange?: (ids: string[]) => void;
}) {
  const [deviceNames, setDeviceNames] = useState<Record<string, string>>({});
  const input = useRef<HTMLTextAreaElement>(null);
  const currentSession = useRef(session?.id);
  currentSession.current = session?.id;
  useEffect(() => () => onFocusChange?.(false), [onFocusChange]);
  const [queueOpen, setQueueOpen] = useState(false);
  const [queueEdit, setQueueEdit] = useState<QueueItem[] | null>(null);
  const [queueBusy, setQueueBusy] = useState(false);
  useEffect(() => {
    setQueueOpen(false);
    setQueueEdit(null);
  }, [session?.id]);
  const action = primaryAction(session, draft);
  const paused = isPaused(session);
  const running = isRunning(session);
  const stopping = session?.status === 'stopping';
  const queue = session?.queue ?? [];
  const shortcuts = useSlashCommands({
    input,
    draft,
    onDraft,
    selectedSkills,
    onSkillsChange,
    models,
    model,
    onModel,
    disabled: disabled || busy || stopping,
    contextKey: draftKey ?? session?.id,
    commands: [
      ...commands,
      ...(queue.length
        ? [
            {
              name: 'queue',
              description: `查看 ${queue.length} 条排队消息`,
              run: () => setQueueOpen(true),
            },
          ]
        : []),
      ...(running ? [{ name: 'stop', description: '停止当前任务并暂停队列', run: onStop }] : []),
    ],
  });
  const submit = (mode: 'queue' | 'steer') => {
    if (disabled || busy || stopping) return;
    if (!shortcuts.submit() && (draft.trim() || paused)) onSend(mode);
  };
  return (
    <div className="composer-wrap">
      {shortcuts.menu}
      {!!queue.length && (
        <div className="queue-container">
          <button
            className="queue-toggle"
            onClick={() => {
              setQueueOpen(!queueOpen);
              setQueueEdit(null);
            }}
            aria-expanded={queueOpen}
          >
            <ListOrdered size={15} />
            {queue.length} 条排队消息<span>{paused || stopping ? '已暂停' : '按顺序处理'}</span>
            <ChevronDown size={15} />
          </button>
          {queueOpen && (
            <div className="queue-panel">
              {(queueEdit ?? queue).map((item, index, items) => (
                <div className="queue-item" key={item.id}>
                  <span>{index + 1}</span>
                  <div className="queue-message-content">
                    <AttachmentChips
                      files={(item.attachments ?? []).filter(
                        (file) => !item.attachment_ids || item.attachment_ids.includes(file.id),
                      )}
                      onPreview={onFilePreview}
                      onRemove={
                        disabled
                          ? undefined
                          : (id) =>
                              setQueueEdit(
                                items.map((q) =>
                                  q.id === item.id
                                    ? {
                                        ...q,
                                        attachment_ids: (
                                          q.attachment_ids ??
                                          q.attachments?.map((file) => file.id) ??
                                          []
                                        ).filter((file) => file !== id),
                                      }
                                    : q,
                                ),
                              )
                      }
                    />
                    <textarea
                      aria-label={`排队消息 ${index + 1}`}
                      value={item.content}
                      onChange={(e) =>
                        setQueueEdit(
                          items.map((q) =>
                            q.id === item.id ? { ...q, content: e.target.value } : q,
                          ),
                        )
                      }
                    />
                    <div className="queue-skill-controls">
                      {item.device_selection?.devices.map((device) => (
                        <span className="skill-chip device-chip" key={device.id}>
                          <Monitor size={12} />
                          {device.name}
                        </span>
                      ))}
                      {session && (
                        <AttachmentPicker
                          ensureSession={async () => session.id}
                          count={item.attachment_ids?.length ?? item.attachments?.length ?? 0}
                          imageCount={
                            (item.attachments ?? []).filter(
                              (file) =>
                                (!item.attachment_ids || item.attachment_ids.includes(file.id)) &&
                                isImageAttachment(file),
                            ).length
                          }
                          disabled={disabled}
                          onAdded={(id, file) => {
                            if (id !== currentSession.current) return;
                            setQueueEdit((current) =>
                              (current ?? items).map((q) =>
                                q.id === item.id
                                  ? {
                                      ...q,
                                      attachments: [...(q.attachments ?? []), file],
                                      attachment_ids: [
                                        ...new Set([
                                          ...(q.attachment_ids ??
                                            q.attachments?.map((entry) => entry.id) ??
                                            []),
                                          file.id,
                                        ]),
                                      ],
                                    }
                                  : q,
                              ),
                            );
                          }}
                        />
                      )}
                      {(item.skill_names ?? item.skills?.map((skill) => skill.name) ?? []).map(
                        (name) => (
                          <span className="skill-chip" key={name}>
                            <BookOpen size={12} />
                            {name}
                            <button
                              type="button"
                              aria-label={`移除排队技能 ${name}`}
                              disabled={disabled}
                              onClick={() =>
                                setQueueEdit(
                                  items.map((q) =>
                                    q.id === item.id
                                      ? {
                                          ...q,
                                          skill_names: (
                                            q.skill_names ??
                                            q.skills?.map((skill) => skill.name) ??
                                            []
                                          ).filter((skill) => skill !== name),
                                        }
                                      : q,
                                  ),
                                )
                              }
                            >
                              <X size={11} />
                            </button>
                          </span>
                        ),
                      )}
                      <SkillsPicker
                        selected={item.skill_names ?? item.skills?.map((skill) => skill.name) ?? []}
                        onChange={(names) =>
                          setQueueEdit(
                            items.map((q) => (q.id === item.id ? { ...q, skill_names: names } : q)),
                          )
                        }
                        disabled={disabled}
                      />
                    </div>
                  </div>
                  <button
                    className="icon-button"
                    aria-label={`上移排队消息 ${index + 1}`}
                    disabled={index === 0 || disabled}
                    onClick={() => {
                      const next = [...items];
                      [next[index - 1], next[index]] = [next[index], next[index - 1]];
                      setQueueEdit(next);
                    }}
                  >
                    <ArrowUp size={15} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`移除排队消息 ${index + 1}`}
                    disabled={disabled}
                    onClick={() => setQueueEdit(items.filter((q) => q.id !== item.id))}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              ))}
              {queueEdit && (
                <div className="queue-actions">
                  <button className="text-button" onClick={() => setQueueEdit(null)}>
                    放弃编辑
                  </button>
                  <button
                    className="primary"
                    disabled={
                      disabled || queueBusy || queueEdit.some((item) => !item.content.trim())
                    }
                    onClick={async () => {
                      setQueueBusy(true);
                      try {
                        await onQueue(queueEdit);
                        setQueueEdit(null);
                      } catch {
                        /* Preserve the edit while the parent displays the API error. */
                      } finally {
                        setQueueBusy(false);
                      }
                    }}
                  >
                    保存队列
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit('queue');
        }}
      >
        <label className="sr-only" htmlFor="task-draft">
          描述你的任务
        </label>
        {!!attachments.length && (
          <AttachmentChips
            files={attachments}
            onPreview={onFilePreview}
            onRemove={onAttachmentRemove}
          />
        )}
        <div
          className={`composer-input${selectedSkills.length || selectedDevices.length ? ' has-skills' : ''}`}
        >
          <textarea
            ref={input}
            {...shortcuts.inputProps}
            id="task-draft"
            onFocus={() => {
              shortcuts.inputProps.onFocus();
              onFocusChange?.(true);
            }}
            onBlur={() => {
              shortcuts.inputProps.onBlur();
              onFocusChange?.(false);
            }}
            value={draft}
            placeholder={paused ? '补充信息，或直接继续处理原任务…' : '想做什么，写在这里…'}
            rows={3}
            onKeyDown={(event) => {
              if (shortcuts.onKeyDown(event)) return;
              if (
                event.key === 'Enter' &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                event.keyCode !== 229
              ) {
                event.preventDefault();
                submit('queue');
              }
            }}
          />
          {!!(selectedSkills.length || selectedDevices.length) && (
            <div className="draft-skills">
              {selectedDevices.map((id) => (
                <span className="skill-chip device-chip" key={id}>
                  <Monitor size={13} />
                  <span className="skill-chip-name">
                    {deviceNames[id] || `设备 ${id.slice(-8)}`}
                  </span>
                  <button
                    type="button"
                    aria-label={`移除设备 ${deviceNames[id] || id.slice(-8)}`}
                    disabled={disabled || busy}
                    onClick={() => onDevicesChange(selectedDevices.filter((item) => item !== id))}
                  >
                    <X size={12} />
                  </button>
                </span>
              ))}
              {selectedSkills.map((name) => (
                <span className="skill-chip" key={name}>
                  <BookOpen size={13} />
                  <span className="skill-chip-name" title={name}>
                    {name}
                  </span>
                  <button
                    type="button"
                    aria-label={`移除技能 ${name}`}
                    onClick={() => onSkillsChange(selectedSkills.filter((item) => item !== name))}
                  >
                    <X size={12} />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
        <div className={`composer-toolbar${running ? ' has-running-task' : ''}`}>
          <div className="composer-tools">
            {ensureSession && onAttachmentAdded && (
              <AttachmentPicker
                ensureSession={ensureSession}
                onAdded={onAttachmentAdded}
                count={attachments.length}
                imageCount={attachments.filter(isImageAttachment).length}
                labelled
                disabled={disabled || busy}
              />
            )}
            <SkillsPicker
              selected={selectedSkills}
              onChange={onSkillsChange}
              disabled={disabled || busy}
            />
            {collaboration && (
              <DevicesPicker
                selected={selectedDevices}
                onChange={onDevicesChange}
                onNames={setDeviceNames}
                disabled={disabled || busy}
              />
            )}
          </div>
          <div className="model-picker">
            <ModelPicker
              models={models}
              value={model}
              disabled={disabled || busy}
              onChange={onModel}
              onManage={onManageProviders}
            />
          </div>
          <div className="composer-actions">
            {onTranscript && (
              <VoiceInput
                draftKey={draftKey ?? session?.id ?? 'new'}
                disabled={disabled}
                onTranscript={onTranscript}
              />
            )}
            {running && (
              <>
                <button
                  type="button"
                  className="steer-button"
                  onClick={() => submit('steer')}
                  disabled={disabled || busy || !draft.trim()}
                >
                  补充当前任务
                </button>
                <button
                  type="button"
                  className="stop-button"
                  aria-label="停止当前任务并暂停队列"
                  onClick={onStop}
                  disabled={disabled || busy}
                >
                  <Square size={13} fill="currentColor" />
                </button>
              </>
            )}
            <button
              className="primary send-button"
              disabled={disabled || busy || stopping || (!draft.trim() && !paused)}
            >
              {busy || stopping ? (
                <LoaderCircle size={16} className="spin" />
              ) : paused ? (
                <ArrowDown size={16} />
              ) : (
                <Send size={15} />
              )}
              <span>{action}</span>
            </button>
          </div>
        </div>
      </form>
      <div className="composer-footnote">
        <button
          type="button"
          className="workspace-button composer-workspace"
          title={workspace || '未选择工作区时，自动保存到任务文件夹'}
          onClick={onWorkspace}
          disabled={!!session?.messages?.length || !!session?.active_task}
        >
          <FolderOpen size={14} />
          <span>
            {workspace
              ? session && !session.workspace_explicit
                ? '任务文件夹'
                : workspace.split(/[\\/]/).filter(Boolean).pop()
              : '选择工作区'}
          </span>
          {!session?.messages?.length && !session?.active_task && <ChevronDown size={12} />}
        </button>
        <span>Enter 发送 · Shift + Enter 换行</span>
      </div>
    </div>
  );
}

export function WorkspaceDialog({
  value,
  onSave,
  onClose,
}: {
  value: string;
  onSave: (value: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState(value);
  const [listing, setListing] = useState<Awaited<ReturnType<typeof api.directories>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const browse = async (target?: string) => {
    setLoading(true);
    setError('');
    try {
      const result = await api.directories(target);
      setListing(result);
      setPath(result.path);
    } catch (e) {
      setError(e instanceof Error ? e.message : '无法浏览目录');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void browse(value || undefined);
  }, []);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-title"
        onClick={(e) => e.stopPropagation()}
      >
        <button className="icon-button modal-close" aria-label="关闭" onClick={onClose}>
          <X size={18} />
        </button>
        <FolderOpen size={26} />
        <h2 id="workspace-title">选择工作区</h2>
        <p className="muted">浏览当前执行设备上的目录。任务开始后，会话将固定使用该目录。</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSave(path.trim());
          }}
        >
          <label className="field-label">
            目录路径
            <div className="directory-input">
              <input
                autoFocus
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="输入目录路径"
              />
              <button
                type="button"
                className="text-button"
                disabled={loading}
                onClick={() => void browse(path)}
              >
                浏览
              </button>
            </div>
          </label>
          {error && (
            <p className="error-banner" role="alert">
              {error}
            </p>
          )}
          {listing && (
            <div className="directory-list">
              <button
                type="button"
                disabled={loading || listing.path === listing.parent}
                onClick={() => void browse(listing.parent)}
              >
                <ArrowUp size={14} />
                上一级目录
              </button>
              {listing.directories.map((directory) => (
                <button
                  key={directory.path}
                  type="button"
                  disabled={loading}
                  onClick={() => void browse(directory.path)}
                >
                  <FolderOpen size={14} />
                  {directory.name}
                </button>
              ))}
              {!listing.directories.length && <p className="muted small">此目录没有子目录</p>}
            </div>
          )}
          <p className="muted small">目录可用性与信任状态由服务验证。</p>
          <div className="directory-actions">
            <button type="button" className="text-button" onClick={() => onSave('')}>
              使用任务文件目录
            </button>
            <button className="primary" type="submit" disabled={loading || !path.trim()}>
              使用此工作区
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
