import { useEffect, useState } from 'react';
import { ArrowLeft, FileDiff, RefreshCw, RotateCcw, X } from 'lucide-react';
import { ApiError, request } from '../api';
import type { Session } from '../types';

type ChangeFile = {
  path: string;
  change: 'created' | 'modified' | 'deleted';
  current: { exists: boolean; kind: string; size: number };
  conflict: boolean;
};
type ChangeGroup = {
  id: string;
  turn_id: string;
  state: 'current' | 'undone' | 'partial' | 'changed' | 'unknown';
  updated_at: string | number;
  operation_count: number;
  operations: { id: string; tool: string; action: string }[];
  files: ChangeFile[];
  direct_undo: boolean;
};
type Checkpoint = {
  hash: string;
  short_hash: string;
  timestamp: string | number;
  reason: string;
  files_changed: number;
  insertions: number;
  deletions: number;
};
type ChangeAction = {
  group_id?: string;
  action: 'undo' | 'restore' | 'checkpoint' | 'prune' | 'clear' | 'recover';
  checkpoint_hash?: string;
  intent_id?: string;
};
type Recovery = { id: string; status: string; created_at: string | number; target_paths: string[] };
type CheckpointStorage = {
  workspace: string;
  shared_store_bytes: number;
  workspace_snapshot_count: number;
  pinned_count: number;
  policy: { retention_days: number; max_snapshots: number; max_total_size_mb: number };
  affected_checkpoints: { hash: string; created_at: string | number; session_id: string }[];
  blocked_by_recovery: boolean;
};
type ChangePreview = ChangeAction & {
  confirmation_token: string;
  workspace: string;
  device: string;
  files: ChangeFile[];
  diff: string;
  conflicts: string[];
  context_impact: { mode: string; scope: string; available: boolean; message_count: number };
  backup_policy: string;
  checkpoint_maintenance?: CheckpointStorage;
  diff_truncated?: boolean;
  workspace_trust_required?: boolean;
};
const stateLabels = {
  current: '当前变更',
  undone: '已撤销',
  partial: '部分已撤销',
  changed: '之后有改动',
  unknown: '待核对',
};
const changeLabels = { created: '新增', modified: '修改', deleted: '删除' };
const at = (value: string | number) => {
  const time = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(time.getTime()) ? '' : time.toLocaleString('zh-CN');
};

export function ChangesPanel({
  session,
  onApplied,
  onClose,
}: {
  session: Session;
  onApplied: (session: Session) => void;
  onClose: () => void;
}) {
  const [groups, setGroups] = useState<ChangeGroup[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [recoveries, setRecoveries] = useState<Recovery[]>([]);
  const [storage, setStorage] = useState<CheckpointStorage | null>(null);
  const [trustAccepted, setTrustAccepted] = useState(false);
  const [preview, setPreview] = useState<ChangePreview | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const executing = ['running', 'waiting', 'stopping'].includes(session.status);
  const root = `/sessions/${encodeURIComponent(session.id)}/changes`;
  const load = async () => {
    setLoading(true);
    try {
      const result = await request<{
        groups: ChangeGroup[];
        checkpoints: Checkpoint[];
        recoveries?: Recovery[];
      }>(root);
      setGroups(result.groups);
      setCheckpoints(result.checkpoints);
      setRecoveries(result.recoveries ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取变更失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void load();
  }, [session.id]);
  const prepare = async (action: ChangeAction) => {
    setTrustAccepted(false);
    setBusy(true);
    setError('');
    setNotice('');
    try {
      setPreview({
        ...(await request<ChangePreview>(`${root}/preview`, 'POST', action)),
        ...action,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : '无法准备变更预览');
    } finally {
      setBusy(false);
    }
  };
  const apply = async (action: ChangeAction, token?: string) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      if (token && preview?.workspace_trust_required) {
        if (!trustAccepted) throw new Error('请先确认是否信任所示工作目录。');
        await request(`${root}/trust`, 'POST', {
          workspace: preview.workspace,
          confirmation_token: token,
        });
      }
      const result = await request<{ success: boolean; session: Session }>(
        `${root}/apply`,
        'POST',
        { ...action, ...(token ? { confirmation_token: token } : {}) },
      );
      if (!result.success) throw new Error('变更未完整完成，请刷新并检查文件状态。');
      onApplied(result.session);
      setPreview(null);
      setNotice(
        {
          restore: '已恢复变更。',
          checkpoint: '已恢复到所选记录。',
          recover: '已恢复到操作前状态。',
          prune: '已清理过期恢复记录。',
          clear: '已清空当前工作区可清理的恢复记录。',
          undo: '已撤销所选变更。',
        }[action.action],
      );
      setStorage(null);
      await load();
      if (storage) {
        try {
          setStorage(await request<CheckpointStorage>(`${root}/status`));
        } catch {
          /* A later refresh can reload storage state. */
        }
      }
    } catch (e) {
      if (
        e instanceof ApiError &&
        ['CONFIRMATION_REQUIRED', 'WORKSPACE_UNTRUSTED'].includes(e.code)
      )
        await prepare(action);
      else setError(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="detail-drawer changes-panel"
        role="dialog"
        aria-modal="true"
        aria-label="会话变更"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-heading">
          <div>
            <FileDiff size={20} />
            <h2>变更</h2>
          </div>
          <button type="button" className="icon-button" aria-label="关闭变更面板" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        {error && (
          <p className="error-banner" role="alert">
            {error}
          </p>
        )}
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        {executing && (
          <p className="notice">任务仍在执行。停止任务并等待收尾后，可以撤销或恢复文件变更。</p>
        )}
        {preview ? (
          <>
            <button
              type="button"
              className="text-button"
              disabled={busy || loading}
              onClick={() => setPreview(null)}
            >
              <ArrowLeft size={15} />
              返回变更列表
            </button>
            <h3>
              {
                {
                  restore: '恢复这些变更',
                  checkpoint: '恢复到所选记录',
                  undo: '撤销这些变更',
                  recover: '处理中断的恢复操作',
                  prune: '清理过期恢复记录',
                  clear: '清空工作区恢复记录',
                }[preview.action]
              }
            </h3>
            <p className="drawer-description">{preview.workspace}</p>
            {preview.workspace_trust_required && (
              <section className="workspace-trust-panel">
                <h3>信任这个工作目录</h3>
                <p>M-Claw 需要在下面的目录中读取、修改文件并运行命令，以完成你确认的操作。</p>
                <code>{preview.workspace}</code>
                <label>
                  <input
                    type="checkbox"
                    checked={trustAccepted}
                    onChange={(event) => setTrustAccepted(event.target.checked)}
                  />
                  <span>我信任此目录，并允许 M-Claw 在此目录中执行这些操作</span>
                </label>
              </section>
            )}
            {!!preview.conflicts.length && (
              <section className="conflict-panel">
                <h3>请核对后续修改</h3>
                <ul>
                  {preview.conflicts.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </section>
            )}
            <ul className="change-file-list">
              {preview.files.map((file) => (
                <li key={file.path}>
                  <span>{changeLabels[file.change]}</span>
                  <code>{file.path}</code>
                </li>
              ))}
            </ul>
            {preview.checkpoint_maintenance ? (
              <section className="review-panel">
                <p>
                  这次将删除当前工作区的{' '}
                  {preview.checkpoint_maintenance.affected_checkpoints.length}{' '}
                  份恢复记录。删除后，这些记录将无法用于恢复文件。
                </p>
                <p className="muted">
                  所有工作区共享存储共占用{' '}
                  {(preview.checkpoint_maintenance.shared_store_bytes / 1024 / 1024).toFixed(1)}{' '}
                  MB。
                </p>
                {preview.checkpoint_maintenance.blocked_by_recovery && (
                  <p role="alert" className="inline-error">
                    还有未完成的恢复操作，请先恢复到操作前状态。
                  </p>
                )}
                <ul>
                  {preview.checkpoint_maintenance.affected_checkpoints.map((item) => (
                    <li key={item.hash}>{at(item.created_at)}</li>
                  ))}
                </ul>
              </section>
            ) : (
              <p className="muted">
                {preview.context_impact.available
                  ? `${['restore', 'checkpoint', 'recover'].includes(preview.action) ? '恢复' : '撤销'}后，会同步调整相关的 ${preview.context_impact.message_count} 条会话记录。`
                  : '这次操作的会话记录影响无法完整核对，请仔细检查变更内容。'}
              </p>
            )}
            {preview.backup_policy && <p className="muted">{preview.backup_policy}</p>}
            {!preview.checkpoint_maintenance && (
              <details className="plain-details" open>
                <summary>文件差异</summary>
                <pre className="file-content diff-content">
                  {preview.diff || '没有文本差异，文件状态请以上方列表为准。'}
                </pre>
              </details>
            )}
            {preview.diff_truncated && (
              <p className="notice">差异较大，这里只显示部分内容，请核对涉及的全部文件。</p>
            )}
            <footer className="drawer-footer">
              <button type="button" className="text-button" onClick={() => setPreview(null)}>
                取消
              </button>
              <button
                type="button"
                className="primary"
                disabled={
                  busy ||
                  loading ||
                  executing ||
                  preview.checkpoint_maintenance?.blocked_by_recovery ||
                  (preview.workspace_trust_required && !trustAccepted)
                }
                onClick={() =>
                  void apply(
                    {
                      action: preview.action,
                      ...(preview.intent_id ? { intent_id: preview.intent_id } : {}),
                      ...(preview.group_id ? { group_id: preview.group_id } : {}),
                      ...(preview.checkpoint_hash
                        ? { checkpoint_hash: preview.checkpoint_hash }
                        : {}),
                    },
                    preview.confirmation_token,
                  )
                }
              >
                {busy
                  ? '正在处理…'
                  : preview.workspace_trust_required
                    ? `信任此目录并${{ restore: '恢复', checkpoint: '恢复', recover: '恢复到操作前', undo: '撤销', prune: '清理', clear: '清空' }[preview.action]}`
                    : {
                        restore: '确认恢复',
                        checkpoint: '确认恢复',
                        recover: '恢复到操作前',
                        undo: '确认撤销',
                        prune: '确认清理',
                        clear: '确认清空',
                      }[preview.action]}
              </button>
            </footer>
          </>
        ) : (
          <>
            <div className="table-toolbar">
              <span>本会话的文件修改</span>
              <button
                type="button"
                className="icon-button"
                aria-label="刷新变更"
                disabled={busy || loading}
                onClick={() => void load()}
              >
                <RefreshCw size={15} />
              </button>
            </div>
            {recoveries.map((recovery) => (
              <section className="conflict-panel" key={recovery.id}>
                <h3>上次恢复操作未完成</h3>
                <p className="muted">
                  {at(recovery.created_at)} · 先恢复到操作前的文件和会话状态，再继续处理变更。
                </p>
                <ul>
                  {recovery.target_paths.map((path) => (
                    <li key={path}>
                      <code>{path}</code>
                    </li>
                  ))}
                </ul>
                <button
                  className="secondary"
                  disabled={busy || loading || executing}
                  onClick={() => void prepare({ action: 'recover', intent_id: recovery.id })}
                >
                  查看恢复方案
                </button>
              </section>
            ))}
            {loading ? (
              <p className="list-empty">正在读取变更…</p>
            ) : (
              !groups.length && <p className="list-empty">这份会话还没有可查看的文件变更。</p>
            )}
            {groups.map((group, index) => (
              <section className="change-group" key={group.id}>
                <div className="change-group-heading">
                  <strong>{stateLabels[group.state]}</strong>
                  <span>{at(group.updated_at)}</span>
                </div>
                <p className="muted">
                  {group.files.length} 个文件 · {group.operation_count} 次操作
                </p>
                <ul className="change-file-list">
                  {group.files.map((file) => (
                    <li key={file.path}>
                      <span>{changeLabels[file.change]}</span>
                      <code>{file.path}</code>
                      {file.conflict && <small>之后有修改</small>}
                    </li>
                  ))}
                </ul>
                <div className="row-actions">
                  <button
                    type="button"
                    className="text-button"
                    disabled={busy || loading || executing}
                    onClick={() =>
                      void prepare({
                        group_id: group.id,
                        action: group.state === 'undone' ? 'restore' : 'undo',
                      })
                    }
                  >
                    查看差异
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy || loading || executing}
                    onClick={() =>
                      group.direct_undo && group.state !== 'undone'
                        ? void apply({ group_id: group.id, action: 'undo' })
                        : void prepare({
                            group_id: group.id,
                            action: group.state === 'undone' ? 'restore' : 'undo',
                          })
                    }
                  >
                    <RotateCcw size={14} />
                    {group.state === 'undone'
                      ? '恢复'
                      : index === 0 && group.direct_undo
                        ? '撤销最近变更'
                        : '撤销'}
                  </button>
                </div>
              </section>
            ))}
            {!!checkpoints.length && (
              <details className="plain-details">
                <summary>更多恢复记录</summary>
                <p className="muted">选择一份记录，先检查将要恢复的文件差异。</p>
                {checkpoints.map((checkpoint) => (
                  <div className="setting-row" key={checkpoint.hash}>
                    <div>
                      <strong>
                        {/[\u4e00-\u9fff]/.test(checkpoint.reason)
                          ? checkpoint.reason
                          : '自动保存记录'}
                      </strong>
                      <small>
                        {at(checkpoint.timestamp)} · {checkpoint.files_changed} 个文件
                      </small>
                    </div>
                    <button
                      type="button"
                      className="text-button"
                      disabled={busy || loading || executing}
                      onClick={() =>
                        void prepare({ action: 'checkpoint', checkpoint_hash: checkpoint.hash })
                      }
                    >
                      查看
                    </button>
                  </div>
                ))}
              </details>
            )}
            <details
              className="plain-details"
              onToggle={async (event) => {
                if (!event.currentTarget.open || storage || loading || busy) return;
                setBusy(true);
                try {
                  setStorage(await request<CheckpointStorage>(`${root}/status`));
                } catch (e) {
                  setError(e instanceof Error ? e.message : '读取存储状态失败');
                } finally {
                  setBusy(false);
                }
              }}
            >
              <summary
                aria-disabled={loading || busy}
                onClick={(event) => {
                  if (loading || busy) event.preventDefault();
                }}
              >
                恢复记录与存储
              </summary>
              {storage ? (
                <>
                  <dl className="compact-definition">
                    <dt>当前工作区</dt>
                    <dd>
                      {storage.workspace_snapshot_count} 份记录，{storage.pinned_count} 份受保护
                    </dd>
                    <dt>所有工作区共享占用</dt>
                    <dd>{(storage.shared_store_bytes / 1024 / 1024).toFixed(1)} MB</dd>
                    <dt>保留策略</dt>
                    <dd>
                      {storage.policy.retention_days} 天 · 最多 {storage.policy.max_snapshots} 份 ·
                      共享存储上限 {storage.policy.max_total_size_mb} MB
                    </dd>
                  </dl>
                  {storage.blocked_by_recovery && (
                    <p className="inline-error">有未完成的恢复操作，暂时无法清理。</p>
                  )}
                  <div className="row-actions">
                    <button
                      className="secondary"
                      disabled={busy || loading || executing || storage.blocked_by_recovery}
                      onClick={() => void prepare({ action: 'prune' })}
                    >
                      清理过期记录
                    </button>
                    <button
                      className="text-button danger-text"
                      disabled={busy || loading || executing || storage.blocked_by_recovery}
                      onClick={() => void prepare({ action: 'clear' })}
                    >
                      清空工作区记录
                    </button>
                  </div>
                </>
              ) : (
                <p className="muted">正在读取存储状态…</p>
              )}
            </details>
          </>
        )}
      </aside>
    </div>
  );
}
