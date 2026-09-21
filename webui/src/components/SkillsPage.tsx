import { useCallback, useEffect, useState } from 'react';
import {
  BookOpen,
  Check,
  ChevronRight,
  Download,
  File,
  Folder,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { skillsApi } from '../api';
import type { SkillDetail, SkillDraft, SkillFile, SkillSummary } from '../types';

export function sourceLabel(source: unknown, detail = false): string {
  if (typeof source === 'string')
    return (
      (
        {
          local: '本机',
          builtin: '内置',
          bundled: '内置',
          agent_created: '本机创建',
          github: 'GitHub',
          skills_sh: 'Skills.sh',
          clawhub: 'ClawHub',
          community: '社区',
          official: '官方',
        } as Record<string, string>
      )[source] ?? (!detail && /^[a-z][a-z0-9_-]*$/.test(source) ? '其他来源' : source)
    );
  if (source && typeof source === 'object') {
    const value = source as Record<string, unknown>;
    return sourceLabel(
      value.url || value.path || value.location || value.original || value.type || '来源已记录',
      detail,
    );
  }
  return '本机';
}
export const skillBody = (content: string) =>
  content.replace(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '');

export function ReviewValue({ value }: { value: unknown }) {
  if (value == null) return null;
  if (typeof value === 'string' || typeof value === 'number')
    return (
      <p>
        {(
          {
            safe: '通过',
            warning: '需要留意',
            dangerous: '存在风险',
            low: '低',
            medium: '中',
            high: '高',
            critical: '高',
            prepared: '待确认',
          } as Record<string, string>
        )[String(value)] ?? String(value)}
      </p>
    );
  if (typeof value === 'boolean') return <span>{value ? '是' : '否'}</span>;
  if (Array.isArray(value))
    return (
      <ul>
        {value.map((item, index) => (
          <li key={index}>
            <ReviewValue value={item} />
          </li>
        ))}
      </ul>
    );
  const labels: Record<string, string> = {
    summary: '摘要',
    status: '状态',
    findings: '审查发现',
    warnings: '注意事项',
    errors: '问题',
    reason: '原因',
    risk_level: '风险程度',
    blocked: '限制',
    dependencies: '依赖',
    missing: '缺少',
    required: '需要',
    tools: '工具',
    packages: '软件包',
    note: '说明',
    description: '说明',
    capability: '能力',
    platform: '平台',
    result: '结果',
    message: '说明',
    severity: '程度',
    approved: '已通过',
    safe: '可用',
    needs_review: '需要审查',
    verdict: '检查结论',
    install_allowed: '允许安装',
    checked_at: '检查时间',
    file: '文件',
    path: '路径',
    line: '行号',
    evidence: '依据',
    recommendation: '建议',
    install_policy_reason: '安装建议',
    category: '类型',
    scan_report: '详细报告',
  };
  return (
    <dl className="review-values">
      {Object.entries(value as Record<string, unknown>)
        .filter(
          ([key, item]) =>
            !['pattern_id', 'schema_version', 'scan_report'].includes(key) &&
            item !== null &&
            item !== undefined &&
            item !== '',
        )
        .map(([key, item]) => (
          <div key={key}>
            <dt>{labels[key] ?? key.replace(/_/g, ' ')}</dt>
            <dd>
              <ReviewValue value={item} />
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function DraftFiles({ id, files }: { id?: string; files: unknown }) {
  const [selected, setSelected] = useState<{ path: string; content: string } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  if (!id || !Array.isArray(files)) return <ReviewValue value={files} />;
  return (
    <div className="draft-files">
      <div className="draft-file-list">
        {files
          .filter((file): file is string => typeof file === 'string')
          .map((file) => (
            <button
              type="button"
              className="text-button"
              key={file}
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError('');
                try {
                  setSelected(await skillsApi.draftFile(id, file));
                } catch (e) {
                  setError(e instanceof Error ? e.message : '读取文件失败');
                } finally {
                  setBusy(false);
                }
              }}
            >
              <File size={14} />
              {file}
            </button>
          ))}
      </div>
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      {selected && (
        <>
          <h4>{selected.path}</h4>
          <pre className="file-content">{selected.content}</pre>
        </>
      )}
    </div>
  );
}

export function SkillsPage({ onUse }: { onUse: (name: string) => void }) {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [file, setFile] = useState<SkillFile | null>(null);
  const [tab, setTab] = useState<'about' | 'files' | 'history'>('about');
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState('');
  const [editingDescription, setEditingDescription] = useState(false);
  const [description, setDescription] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState({ name: '', description: '', content: '' });
  const [installOpen, setInstallOpen] = useState(false);
  const [source, setSource] = useState('');
  const [intent, setIntent] = useState('');
  const [draft, setDraft] = useState<SkillDraft | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const load = useCallback(async () => {
    try {
      setSkills((await skillsApi.list()).skills);
    } catch (e) {
      setError(e instanceof Error ? e.message : '技能加载失败');
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };
  const openSkill = (name: string) =>
    void action(async () => {
      setDetail(await skillsApi.detail(name));
      setTab('about');
      setEditing(false);
      setEditingDescription(false);
      setFile(null);
    });
  const closeDetail = () => {
    setDetail(null);
    setFile(null);
    setEditing(false);
    setEditingDescription(false);
    setDeleteOpen(false);
  };
  const visible = skills.filter((skill) =>
    `${skill.name} ${skill.description}`.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <section className="management-page">
      <div className="page-heading">
        <div>
          <h2>技能</h2>
          <p>给 M-Claw 添加处理特定任务的方法。</p>
        </div>
        <div className="page-actions">
          <button
            className="secondary"
            onClick={() => {
              setCreateOpen(true);
              setError('');
            }}
          >
            <Plus size={15} />
            新建技能
          </button>
          <button
            className="primary"
            onClick={() => {
              setInstallOpen(true);
              setError('');
            }}
          >
            <Download size={15} />
            安装技能
          </button>
        </div>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      <div className="table-toolbar">
        <label className="table-search">
          <Search size={15} />
          <input
            aria-label="搜索已安装技能"
            placeholder="搜索名称或说明"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <span>{skills.length} 个技能</span>
        <button
          className="icon-button"
          aria-label="刷新技能"
          disabled={busy}
          onClick={() => void load()}
        >
          <RefreshCw size={15} />
        </button>
      </div>
      <div className="data-table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>来源</th>
              <th aria-label="操作" />
            </tr>
          </thead>
          <tbody>
            {visible.map((skill) => (
              <tr key={skill.name}>
                <td>
                  <button className="row-title" onClick={() => openSkill(skill.name)}>
                    <BookOpen size={17} />
                    <span>
                      <strong>{skill.name}</strong>
                      <small>{skill.description || '暂无说明'}</small>
                    </span>
                  </button>
                </td>
                <td>
                  <button
                    className={`switch ${skill.enabled ? 'on' : ''}`}
                    role="switch"
                    aria-checked={skill.enabled}
                    aria-label={`${skill.name} 启用状态`}
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await skillsApi.update(skill.name, { enabled: !skill.enabled });
                        await load();
                        if (detail?.name === skill.name)
                          setDetail(await skillsApi.detail(skill.name));
                      })
                    }
                  >
                    <span />
                  </button>
                  <span className="table-status">{skill.enabled ? '已启用' : '已停用'}</span>
                </td>
                <td className="source-cell" title={sourceLabel(skill.source)}>
                  {sourceLabel(skill.source)}
                </td>
                <td>
                  <button
                    className="text-button"
                    disabled={!skill.enabled}
                    onClick={() => onUse(skill.name)}
                  >
                    在对话中使用
                    <ChevronRight size={14} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading ? (
          <p className="list-empty">正在读取技能…</p>
        ) : (
          !visible.length && (
            <p className="list-empty">
              {query ? '没有找到匹配的技能。' : '还没有技能。可以从来源安装，也可以自己创建。'}
            </p>
          )
        )}
      </div>
      {detail && (
        <div className="drawer-backdrop" onClick={closeDetail}>
          <aside
            className="detail-drawer"
            role="dialog"
            aria-modal="true"
            aria-label={`${detail.name} 技能详情`}
            onClick={(e) => e.stopPropagation()}
          >
            <header className="drawer-heading">
              <div>
                <BookOpen size={20} />
                <h2>{detail.name}</h2>
              </div>
              <button className="icon-button" aria-label="关闭技能详情" onClick={closeDetail}>
                <X size={18} />
              </button>
            </header>
            {editingDescription ? (
              <form
                className="skill-description-editor"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (busy || !description.trim() || description.trim() === detail.description)
                    return;
                  void action(async () => {
                    const updated = await skillsApi.update(detail.name, {
                      description: description.trim(),
                      version: detail.version,
                    });
                    setDetail(updated);
                    setFile(null);
                    setSkills((items) =>
                      items.map((item) => (item.name === updated.name ? updated : item)),
                    );
                    setEditingDescription(false);
                  });
                }}
              >
                <label className="field-label">
                  简短描述
                  <textarea
                    autoFocus
                    rows={3}
                    value={description}
                    disabled={busy}
                    onChange={(event) => setDescription(event.target.value)}
                  />
                </label>
                <div className="skill-description-actions">
                  <button
                    type="button"
                    className="text-button"
                    disabled={busy}
                    onClick={() => setEditingDescription(false)}
                  >
                    取消
                  </button>
                  <button
                    className="primary"
                    disabled={
                      busy || !description.trim() || description.trim() === detail.description
                    }
                  >
                    保存描述
                  </button>
                </div>
              </form>
            ) : (
              <div className="skill-description">
                <p className="drawer-description">{detail.description}</p>
                <button
                  className="text-button"
                  disabled={busy || editing}
                  onClick={() => {
                    setDescription(detail.description);
                    setEditingDescription(true);
                    setError('');
                  }}
                >
                  <Pencil size={14} />
                  编辑简短描述
                </button>
              </div>
            )}
            <nav className="page-tabs">
              {[
                { id: 'about', label: '说明' },
                { id: 'files', label: '文件' },
                { id: 'history', label: '记录' },
              ].map((item) => (
                <button
                  key={item.id}
                  className={tab === item.id ? 'active' : ''}
                  disabled={busy || editingDescription}
                  onClick={() => {
                    setTab(item.id as typeof tab);
                    setEditing(false);
                  }}
                >
                  {item.label}
                </button>
              ))}
            </nav>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            {tab === 'about' && (
              <>
                <div className="detail-toolbar">
                  <span className="muted small">{detail.enabled ? '已启用' : '已停用'}</span>
                  <button
                    className="text-button"
                    disabled={busy || editingDescription}
                    onClick={() => {
                      setContent(detail.content);
                      setEditing(!editing);
                    }}
                  >
                    <Pencil size={14} />
                    {editing ? '取消编辑' : '编辑技能内容'}
                  </button>
                </div>
                {editing ? (
                  <>
                    <label className="field-label">
                      技能内容
                      <textarea
                        className="code-editor"
                        value={content}
                        onChange={(e) => setContent(e.target.value)}
                        rows={17}
                      />
                    </label>
                    <button
                      className="primary"
                      disabled={busy}
                      onClick={() =>
                        void action(async () => {
                          setDetail(
                            await skillsApi.update(detail.name, {
                              content,
                              version: detail.version,
                            }),
                          );
                          setEditing(false);
                          await load();
                        })
                      }
                    >
                      保存修改
                    </button>
                  </>
                ) : (
                  <div className="markdown skill-markdown">
                    <Markdown remarkPlugins={[remarkGfm]}>{skillBody(detail.content)}</Markdown>
                  </div>
                )}
                <details className="plain-details">
                  <summary>来源与版本</summary>
                  <dl className="compact-definition">
                    <dt>来源</dt>
                    <dd>{sourceLabel(detail.source, true)}</dd>
                    <dt>版本</dt>
                    <dd>{detail.version || '未记录'}</dd>
                  </dl>
                </details>
              </>
            )}
            {tab === 'files' && (
              <div className="skill-files">
                <div className="file-tree">
                  {detail.tree.entries.map((entry) => (
                    <button
                      key={entry.path}
                      disabled={entry.type === 'directory' || busy}
                      onClick={() =>
                        void action(async () => {
                          const next = await skillsApi.file(detail.name, entry.path);
                          setFile(next);
                          setContent(next.content);
                          setEditing(false);
                        })
                      }
                      className={file?.path === entry.path ? 'selected' : ''}
                    >
                      {entry.type === 'directory' ? <Folder size={14} /> : <File size={14} />}
                      <span>{entry.path}</span>
                    </button>
                  ))}
                  {detail.tree.truncated && <p className="muted small">文件较多，仅列出部分。</p>}
                </div>
                {file && (
                  <div className="file-editor">
                    <div className="detail-toolbar">
                      <strong>{file.path}</strong>
                      {file.editable && (
                        <button
                          className="text-button"
                          disabled={busy || editingDescription}
                          onClick={() => setEditing(!editing)}
                        >
                          <Pencil size={14} />
                          {editing ? '取消编辑' : '编辑'}
                        </button>
                      )}
                    </div>
                    {editing ? (
                      <>
                        <textarea
                          aria-label="文件内容"
                          className="code-editor"
                          rows={18}
                          value={content}
                          onChange={(e) => setContent(e.target.value)}
                        />
                        <button
                          className="primary"
                          disabled={busy}
                          onClick={() =>
                            void action(async () => {
                              setFile(
                                await skillsApi.saveFile(detail.name, {
                                  path: file.path,
                                  content,
                                  version: file.version,
                                }),
                              );
                              setEditing(false);
                              setDetail(await skillsApi.detail(detail.name));
                            })
                          }
                        >
                          保存文件
                        </button>
                      </>
                    ) : (
                      <pre className="file-preview">{file.content}</pre>
                    )}
                  </div>
                )}
              </div>
            )}
            {tab === 'history' && (
              <div className="review-panel">
                <ReviewValue value={detail.evolution} />
                {!detail.evolution && <p className="list-empty">还没有变更记录。</p>}
                <details className="plain-details">
                  <summary>技能信息</summary>
                  <ReviewValue value={detail.metadata} />
                </details>
              </div>
            )}
            <footer className="drawer-footer">
              <button className="danger-text text-button" onClick={() => setDeleteOpen(true)}>
                <Trash2 size={14} />
                删除技能
              </button>
              <button
                className="primary"
                disabled={!detail.enabled}
                onClick={() => onUse(detail.name)}
              >
                在对话中使用
              </button>
            </footer>
            {deleteOpen && (
              <div className="inline-confirm">
                <strong>删除 {detail.name}？</strong>
                <p>技能文件会按管理规则删除，已发送任务保留提交时的记录。</p>
                <div>
                  <button className="text-button" onClick={() => setDeleteOpen(false)}>
                    取消
                  </button>
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await skillsApi.remove(detail.name, detail.version);
                        closeDetail();
                        await load();
                      })
                    }
                  >
                    确认删除
                  </button>
                </div>
              </div>
            )}
          </aside>
        </div>
      )}
      {createOpen && (
        <div className="modal-backdrop">
          <section
            className="modal wide-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-skill-title"
          >
            <button
              className="icon-button modal-close"
              aria-label="关闭新建技能"
              onClick={() => setCreateOpen(false)}
            >
              <X size={18} />
            </button>
            <h2 id="create-skill-title">新建技能</h2>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void action(async () => {
                  const result = await skillsApi.create(createForm);
                  setCreateOpen(false);
                  setCreateForm({ name: '', description: '', content: '' });
                  await load();
                  setDetail(result);
                });
              }}
            >
              <div className="form-grid">
                <label className="field-label">
                  名称
                  <input
                    required
                    value={createForm.name}
                    onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
                    placeholder="例如 weekly-report"
                  />
                </label>
                <label className="field-label">
                  中文简介
                  <input
                    required
                    value={createForm.description}
                    onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                    placeholder="这个技能能做什么"
                  />
                </label>
              </div>
              <label className="field-label">
                执行方法
                <textarea
                  required
                  rows={10}
                  className="code-editor"
                  value={createForm.content}
                  onChange={(e) => setCreateForm({ ...createForm, content: e.target.value })}
                  placeholder="说明适用场景、处理步骤和输出要求，支持 Markdown。"
                />
              </label>
              <button className="primary" disabled={busy}>
                创建技能
              </button>
            </form>
          </section>
        </div>
      )}
      {installOpen && (
        <div className="drawer-backdrop" onClick={() => setInstallOpen(false)}>
          <aside
            className="detail-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="安装技能"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="drawer-heading">
              <h2>安装技能</h2>
              <button
                className="icon-button"
                aria-label="关闭安装技能"
                onClick={() => setInstallOpen(false)}
              >
                <X size={18} />
              </button>
            </header>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            {!draft ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void action(async () =>
                    setDraft(await skillsApi.prepare(source, intent || undefined)),
                  );
                }}
              >
                <label className="field-label">
                  来源
                  <input
                    required
                    value={source}
                    onChange={(e) => setSource(e.target.value)}
                    placeholder="仓库地址、下载链接或本机路径"
                  />
                </label>
                <label className="field-label">
                  你希望用它做什么（可选）
                  <textarea rows={3} value={intent} onChange={(e) => setIntent(e.target.value)} />
                </label>
                <button className="primary" disabled={busy || !source.trim()}>
                  {busy ? '正在准备…' : '检查并准备安装'}
                </button>
              </form>
            ) : (
              <>
                <div className="draft-summary">
                  <BookOpen size={24} />
                  <div>
                    <h3>{draft.skill_name}</h3>
                    <p>{sourceLabel(draft.source)}</p>
                  </div>
                </div>
                <section className="review-panel">
                  <h3>安装检查</h3>
                  <ReviewValue value={draft.review} />
                  {draft.blocked && (
                    <div className="error-banner">当前检查未通过，请处理提示的问题后重新准备。</div>
                  )}
                </section>
                <details className="plain-details">
                  <summary>依赖与运行条件</summary>
                  <ReviewValue value={draft.dependency_hints} />
                </details>
                <details className="plain-details">
                  <summary>技能内容</summary>
                  <div className="markdown skill-markdown">
                    <Markdown remarkPlugins={[remarkGfm]}>{skillBody(draft.content)}</Markdown>
                  </div>
                </details>
                <details className="plain-details">
                  <summary>随附文件</summary>
                  <DraftFiles id={draft.drafting_id} files={draft.files} />
                </details>
                <div className="drawer-footer">
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await skillsApi.cancel(draft.drafting_id);
                        setDraft(null);
                      })
                    }
                  >
                    取消安装
                  </button>
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() =>
                      void action(async () => setDraft(await skillsApi.draft(draft.drafting_id)))
                    }
                  >
                    <RefreshCw size={14} />
                    重新检查
                  </button>
                  <button
                    className="primary"
                    disabled={busy || draft.blocked || !draft.confirmation_token}
                    onClick={() =>
                      void action(async () => {
                        await skillsApi.confirm(draft.drafting_id, draft.confirmation_token);
                        setDraft(null);
                        setInstallOpen(false);
                        await load();
                      })
                    }
                  >
                    <Check size={14} />
                    确认安装
                  </button>
                </div>
              </>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
