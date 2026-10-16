import { Select } from './Select';
import { useEffect, useState } from 'react';
import { Check, ChevronRight, Download, LoaderCircle, RefreshCw, Settings, X } from 'lucide-react';
import { ApiError, settingsApi, voiceApi } from '../api';
import type { Bootstrap, Diagnostics, MemoryDocument, VoiceStatus } from '../types';
import { OutputSettings } from './OutputSettings';
import { ServiceControls } from './ServiceControls';
import { ChannelsPage } from './ChannelsPage';
import { ConfigurationPanel } from './ConfigurationPanel';

export { DeviceStatus } from './DevicePage';

export function SettingsPage({
  bootstrap,
  onSetup,
  onProviders,
  onShutdown,
}: {
  bootstrap: Bootstrap;
  onSetup: () => void;
  onProviders: () => void;
  onShutdown: () => void;
}) {
  const [tab, setTab] = useState<
    'general' | 'memory' | 'diagnostics' | 'channels' | 'execution' | 'scheduler'
  >('general');
  const [documents, setDocuments] = useState<MemoryDocument[]>([]);
  const [selected, setSelected] = useState('memory');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<MemoryDocument | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [voiceKey, setVoiceKey] = useState('');
  const document = documents.find((item) => item.target === selected);
  useEffect(() => {
    if (tab !== 'memory' || documents.length) return;
    let active = true;
    setBusy(true);
    setError('');
    void settingsApi
      .memory()
      .then((result) => {
        if (active) {
          setDocuments(result.documents);
          setDrafts(
            Object.fromEntries(result.documents.map((item) => [item.target, item.content])),
          );
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [tab, documents.length]);
  const save = async (revision: string) => {
    if (!document) return;
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      const result = await settingsApi.saveMemory(
        document.target,
        drafts[document.target] ?? '',
        revision,
      );
      setDocuments((current) =>
        current.map((item) => (item.target === result.target ? result : item)),
      );
      setDrafts((current) => ({ ...current, [result.target]: result.content }));
      setConflict(null);
      setSaved(true);
    } catch (e) {
      if (e instanceof ApiError && e.code === 'MEMORY_CONFLICT' && e.current)
        setConflict(e.current as MemoryDocument);
      setError(e instanceof Error ? e.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };
  const diagnose = async () => {
    setBusy(true);
    setError('');
    try {
      setDiagnostics(await settingsApi.diagnostics());
    } catch (e) {
      setError(e instanceof Error ? e.message : '检查失败');
    } finally {
      setBusy(false);
    }
  };
  const exportReport = () => {
    if (!diagnostics) return;
    const url = URL.createObjectURL(
      new Blob([diagnostics.report], { type: 'text/plain;charset=utf-8' }),
    );
    const anchor = window.document.createElement('a');
    anchor.href = url;
    anchor.download = 'mclaw-diagnostics.txt';
    anchor.click();
    URL.revokeObjectURL(url);
  };
  return (
    <section className="management-page settings-page">
      <div className="page-heading">
        <div>
          <h2>设置</h2>
        </div>
      </div>
      <nav className="page-tabs">
        <button className={tab === 'general' ? 'active' : ''} onClick={() => setTab('general')}>
          常规
        </button>
        <button className={tab === 'execution' ? 'active' : ''} onClick={() => setTab('execution')}>
          任务执行
        </button>
        <button className={tab === 'scheduler' ? 'active' : ''} onClick={() => setTab('scheduler')}>
          定时任务
        </button>
        <button
          className={tab === 'channels' ? 'active' : ''}
          onClick={() => {
            setTab('channels');
            setError('');
          }}
        >
          消息渠道
        </button>
        <button
          className={tab === 'memory' ? 'active' : ''}
          onClick={() => {
            setTab('memory');
            setError('');
          }}
        >
          记忆与偏好
        </button>
        <button
          className={tab === 'diagnostics' ? 'active' : ''}
          onClick={() => {
            setTab('diagnostics');
            setError('');
          }}
        >
          运行检查
        </button>
      </nav>
      {tab === 'channels' && <ChannelsPage />}
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {tab === 'general' && (
        <>
          <div className="settings-section">
            <h3>模型与输入</h3>
            <div className="setting-rows">
              <button className="setting-row setting-link" onClick={onProviders}>
                <div>
                  <strong>供应商与模型</strong>
                  <small>添加供应商，管理 API Key 和默认模型</small>
                </div>
                <ChevronRight size={17} />
              </button>
              <button
                className="setting-row setting-link"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError('');
                  try {
                    setVoice(await voiceApi.status());
                    setVoiceKey('');
                  } catch (e) {
                    setError(e instanceof Error ? e.message : '读取语音设置失败');
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <div>
                  <strong>语音输入</strong>
                  <small>语音转写服务与 API Key</small>
                </div>
                <ChevronRight size={17} />
              </button>
            </div>
          </div>
          {bootstrap.output_root && <OutputSettings initial={bootstrap.output_root} />}
        </>
      )}
      <ConfigurationPanel
        page={tab === 'channels' || tab === 'diagnostics' ? null : tab}
        onSaved={(snapshot) => {
          const fields = snapshot.groups.flatMap((group) => group.fields);
          setDocuments((current) =>
            current.map((doc) => ({
              ...doc,
              limit: Number(
                fields.find(
                  (field) =>
                    field.path ===
                    (doc.target === 'memory'
                      ? 'memory.memory_char_limit'
                      : 'memory.user_char_limit'),
                )?.value ?? doc.limit,
              ),
            })),
          );
        }}
      />
      {tab === 'general' && (
        <div className="settings-section">
          <h3>应用</h3>
          <div className="setting-rows">
            <button className="setting-row setting-link" onClick={onSetup}>
              <div>
                <strong>设置向导</strong>
                <small>重新进行初始配置</small>
              </div>
              <ChevronRight size={17} />
            </button>
            <ServiceControls onRequested={onShutdown} />
          </div>
        </div>
      )}
      {voice && (
        <div className="modal-backdrop">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="voice-settings-title"
          >
            <button
              className="icon-button modal-close"
              aria-label="关闭语音设置"
              onClick={() => {
                setVoice(null);
                setVoiceKey('');
              }}
            >
              <X size={18} />
            </button>
            <h2 id="voice-settings-title">语音输入</h2>
            <p>{voice.notice}</p>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            <label className="field-label">
              <span>启用语音输入</span>
              <input
                type="checkbox"
                checked={voice.enabled}
                onChange={(e) => setVoice({ ...voice, enabled: e.target.checked })}
              />
            </label>
            <label className="field-label">
              服务区域
              <Select
                label="服务区域"
                value={voice.provider}
                onChange={(selectedValue) =>
                  setVoice({ ...voice, provider: selectedValue as VoiceStatus['provider'] })
                }
              >
                <option value="qwen">中国内地</option>
                <option value="qwen-intl">国际</option>
              </Select>
            </label>
            <label className="field-label">
              Qwen API Key
              <input
                type="password"
                autoComplete="off"
                value={voiceKey}
                onChange={(e) => setVoiceKey(e.target.value)}
                placeholder={
                  voice.credential_configured ? '已保存，留空复用' : '填写对应区域的 API Key'
                }
              />
            </label>
            <p className="muted">每次最多录音 {voice.max_seconds} 秒。转写文字会先放入草稿。</p>
            <button
              className="primary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError('');
                try {
                  await voiceApi.configure({
                    enabled: voice.enabled,
                    provider: voice.provider,
                    ...(voiceKey.trim() ? { api_key: voiceKey.trim() } : {}),
                  });
                  setVoiceKey('');
                  setVoice(null);
                } catch (e) {
                  setError(e instanceof Error ? e.message : '保存失败');
                } finally {
                  setBusy(false);
                }
              }}
            >
              保存设置
            </button>
          </section>
        </div>
      )}
      {tab === 'memory' && (
        <div className="settings-section memory-editor">
          <h3>记忆与偏好</h3>
          <div className="segmented-control">
            <button
              className={selected === 'memory' ? 'active' : ''}
              onClick={() => {
                setSelected('memory');
                setConflict(null);
                setSaved(false);
              }}
            >
              长期记忆
            </button>
            <button
              className={selected === 'user' ? 'active' : ''}
              onClick={() => {
                setSelected('user');
                setConflict(null);
                setSaved(false);
              }}
            >
              个人偏好
            </button>
          </div>
          {busy && !document ? (
            <p className="list-empty">正在读取…</p>
          ) : (
            document && (
              <>
                <p className="section-description">
                  {selected === 'memory'
                    ? '保存值得在以后任务中参考的信息。'
                    : '告诉 M-Claw 你的习惯、偏好和常用信息。'}
                </p>
                <label className="field-label">
                  {document.label}
                  <textarea
                    className="code-editor"
                    rows={16}
                    value={drafts[document.target] ?? ''}
                    onChange={(e) => {
                      setDrafts((current) => ({ ...current, [document.target]: e.target.value }));
                      setSaved(false);
                    }}
                  />
                </label>
                <div className="editor-status">
                  <span>
                    {Array.from(drafts[document.target] ?? '').length.toLocaleString()} 字
                    {document.limit ? ` / ${document.limit.toLocaleString()}` : ''}
                  </span>
                  {saved && (
                    <span className="saved-label">
                      <Check size={14} />
                      已保存
                    </span>
                  )}
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => void save(document.revision)}
                  >
                    保存修改
                  </button>
                </div>
                {conflict && (
                  <section className="conflict-panel">
                    <h3>内容刚刚有更新</h3>
                    <p>检查下面的最新内容，在上方合并你的修改后再次保存。</p>
                    <textarea
                      aria-label="最新保存的内容"
                      readOnly
                      rows={7}
                      value={conflict.content}
                    />
                    <div className="dialog-actions">
                      <button
                        className="text-button"
                        onClick={() => {
                          setDocuments((current) =>
                            current.map((item) =>
                              item.target === conflict.target ? conflict : item,
                            ),
                          );
                          setDrafts((current) => ({
                            ...current,
                            [conflict.target]: conflict.content,
                          }));
                          setConflict(null);
                          setError('');
                        }}
                      >
                        使用最新内容
                      </button>
                      <button
                        className="primary"
                        disabled={busy}
                        onClick={() => void save(conflict.revision)}
                      >
                        我已合并，重新保存
                      </button>
                    </div>
                  </section>
                )}
                <details className="plain-details">
                  <summary>更新记录</summary>
                  {document.history?.length ? (
                    <ul className="history-records">
                      {document.history.map((item, index) => (
                        <li key={index}>
                          <span>
                            {typeof item.at === 'number'
                              ? new Date(item.at * 1000).toLocaleString('zh-CN')
                              : item.at}
                          </span>
                          <p>{item.summary}</p>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="list-empty">还没有更新记录。</p>
                  )}
                </details>
              </>
            )
          )}
        </div>
      )}
      {tab === 'diagnostics' && (
        <section className="settings-section">
          <h3>运行检查</h3>
          <div className="table-toolbar">
            <span>检查模型、工具和设备连接是否正常。</span>
            <div className="row-actions">
              {diagnostics && (
                <button className="secondary" onClick={exportReport}>
                  <Download size={14} />
                  保存报告
                </button>
              )}
              <button className="primary" disabled={busy} onClick={() => void diagnose()}>
                {busy ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}
                {diagnostics ? '重新检查' : '开始检查'}
              </button>
            </div>
          </div>
          {diagnostics ? (
            <>
              <div className="diagnostic-summary">
                <span>
                  <i className="status-dot" />
                  {diagnostics.summary.passed} 项正常
                </span>
                <span>{diagnostics.summary.warnings} 项需注意</span>
                <span>{diagnostics.summary.failed} 项需处理</span>
              </div>
              <div className="data-table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>检查项</th>
                      <th>结果</th>
                      <th>说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diagnostics.checks.map((item, index) => (
                      <tr key={index}>
                        <td>
                          <strong>{item.name}</strong>
                          <small className="cell-subtitle">{item.section}</small>
                        </td>
                        <td>
                          <span
                            className={`status-label ${item.status === 'OK' ? 'good' : item.status === 'FAIL' ? 'bad' : ''}`}
                          >
                            {item.status === 'OK'
                              ? '正常'
                              : item.status === 'WARN'
                                ? '需注意'
                                : '需处理'}
                          </span>
                        </td>
                        <td>
                          <p className="check-detail">{item.detail}</p>
                          {item.fix && <p className="check-fix">{item.fix}</p>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p className="list-empty">运行检查后，会在这里显示结果。</p>
          )}
        </section>
      )}
    </section>
  );
}
