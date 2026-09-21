import { Select } from './Select';
import { useEffect, useRef, useState } from 'react';
import {
  BookOpen,
  Brain,
  ChevronRight,
  FileText,
  GitBranch,
  Globe,
  History,
  Image,
  KeyRound,
  LoaderCircle,
  Monitor,
  Search,
  Terminal,
  Wrench,
  X,
} from 'lucide-react';
import { ApiError, toolsApi } from '../api';
import type { ManagedTool, ToolCatalog, ToolForm } from '../types';
import { ChannelLogo } from './ChannelLogo';

const ICONS = {
  credentials: KeyRound,
  terminal: Terminal,
  file: FileText,
  memory: Brain,
  skills: BookOpen,
  session_search: History,
  delegation: GitBranch,
  web_search: Search,
  web_extract: Globe,
  vision: Image,
  browser: Monitor,
};
const CREDENTIAL_LABELS: Record<string, string> = {
  TAVILY_API_KEY: 'Tavily',
  FIRECRAWL_API_KEY: 'Firecrawl',
  DASHSCOPE_API_KEY: '通义千问 · 中国区',
  DASHSCOPE_INTL_API_KEY: '通义千问 · 国际区',
  QWEN_API_KEY: '通义千问 · Qwen 凭据',
};

function ToolIcon({ tool }: { tool: ManagedTool }) {
  const Icon = ICONS[tool.id as keyof typeof ICONS] ?? Wrench;
  return (
    <span className="tool-icon">
      {tool.channel ? (
        <ChannelLogo source={tool.channel} size={22} />
      ) : (
        <Icon size={20} strokeWidth={1.6} />
      )}
    </span>
  );
}

function ToolEditor({
  tool,
  revision,
  onSaved,
  onClose,
  onReload,
  enable,
}: {
  tool: ManagedTool;
  revision: string;
  onSaved: (value: ToolCatalog) => void;
  onClose: () => void;
  onReload: () => void;
  enable: boolean;
}) {
  const [backend, setBackend] = useState(tool.backend);
  const [source, setSource] = useState(tool.credential_source);
  const [key, setKey] = useState('');
  const [model, setModel] = useState(tool.model);
  const [enabled, setEnabled] = useState(enable || tool.enabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const selected = tool.backends.find((item) => item.id === backend);
  const choices = selected?.credentials ?? [];
  const configured = choices.filter((item) => item.configured);
  const credential = choices.some((item) => item.id === source)
    ? source
    : (configured[0]?.id ?? choices[0]?.id ?? '');
  const hasModel = tool.id === 'vision' || (tool.id === 'web_search' && backend !== 'tavily');
  const missingCredential =
    choices.length > 0 &&
    !key.trim() &&
    !choices.find((item) => item.id === credential)?.configured;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => previous?.focus();
  }, []);
  const save = async () => {
    setBusy(true);
    setError('');
    try {
      const form: ToolForm = { revision, enabled };
      if (tool.backends.length && (enabled || !missingCredential)) {
        form.backend = backend;
        if (choices.length) {
          form.credential_source = credential;
          if (key.trim()) form.api_key = key.trim();
        }
        if (hasModel) form.model = model;
      }
      onSaved(await toolsApi.save(tool.id, form));
      setKey('');
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '保存失败');
      setConflict(failure instanceof ApiError && failure.code === 'TOOLS_CONFLICT');
    } finally {
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
        className="modal tool-editor"
        ref={dialog}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tool-editor-title"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !busy) {
            event.stopPropagation();
            onClose();
          }
          if (event.key === 'Tab') {
            const elements = Array.from(
              dialog.current?.querySelectorAll<HTMLElement>(
                'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]',
              ) ?? [],
            );
            const first = elements[0],
              last = elements.at(-1);
            if (
              event.shiftKey &&
              (document.activeElement === first || document.activeElement === dialog.current)
            ) {
              event.preventDefault();
              last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <button
          className="icon-button modal-close"
          aria-label="关闭工具设置"
          disabled={busy}
          onClick={onClose}
        >
          <X size={18} />
        </button>
        <div className="tool-editor-heading">
          <ToolIcon tool={tool} />
          <div>
            <h2 id="tool-editor-title">{tool.label}</h2>
            <p>{tool.description}</p>
          </div>
        </div>
        {error && (
          <p role="alert" className="inline-error">
            {error}
            {conflict && (
              <button className="text-button" onClick={onReload}>
                重新加载
              </button>
            )}
          </p>
        )}
        {tool.required ? (
          <p className="tool-required-note">基础能力 · 始终开启</p>
        ) : (
          <label className="setting-row">
            <strong>启用工具</strong>
            <input
              className="tool-switch"
              type="checkbox"
              role="switch"
              checked={enabled}
              disabled={busy}
              onChange={(event) => setEnabled(event.target.checked)}
            />
          </label>
        )}
        {tool.backends.length > 0 && (
          <div className="tool-form-fields">
            <label className="field-label">
              服务来源
              <Select
                label="服务来源"
                value={backend}
                disabled={busy}
                onChange={(selectedValue) => {
                  setBackend(selectedValue);
                  setSource('');
                  setKey('');
                }}
              >
                {tool.backends.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </Select>
            </label>
            {choices.length > 0 && (
              <>
                <label className="field-label">
                  凭据来源
                  <Select
                    label="凭据来源"
                    value={credential}
                    disabled={busy}
                    onChange={(selectedValue) => {
                      setSource(selectedValue);
                      setKey('');
                    }}
                  >
                    {choices.map((item) => (
                      <option key={item.id} value={item.id}>
                        {CREDENTIAL_LABELS[item.id] ?? item.id}
                        {item.configured ? ' · 已保存' : ' · 待填写'}
                      </option>
                    ))}
                  </Select>
                </label>
                <label className="field-label">
                  API Key
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={key}
                    disabled={busy}
                    placeholder={
                      choices.find((item) => item.id === credential)?.configured
                        ? '已保存，留空复用'
                        : '填写 API Key'
                    }
                    onChange={(event) => setKey(event.target.value)}
                  />
                </label>
                <p className="field-hint">保存后，此工具可使用所选凭据。</p>
              </>
            )}
            {hasModel && (
              <label className="field-label">
                模型
                <input
                  value={model}
                  disabled={busy}
                  placeholder={tool.id === 'vision' ? 'qwen3-vl-flash' : '使用默认模型'}
                  onChange={(event) => setModel(event.target.value)}
                />
              </label>
            )}
          </div>
        )}
        {tool.channel && (
          <p className="field-hint">
            在已连接的{tool.channel === 'weixin' ? '微信' : '钉钉'}
            对话中使用。连接账号请前往设置中的消息渠道。
          </p>
        )}
        <details className="plain-details tool-operations">
          <summary>
            包含的操作 <span>{tool.tools.length}</span>
          </summary>
          <ul>
            {tool.tools.map((name) => (
              <li key={name}>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </details>
        <div className="dialog-actions">
          <button className="secondary" disabled={busy} onClick={onClose}>
            关闭
          </button>
          {!tool.required && (
            <button
              className="primary"
              disabled={busy || conflict || (enabled && missingCredential)}
              onClick={() => void save()}
            >
              {busy && <LoaderCircle size={15} className="spin" />}保存
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function ToolsPage() {
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [selected, setSelected] = useState<{ tool: ManagedTool; enable: boolean } | null>(null);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const load = async () => {
    try {
      setCatalog(await toolsApi.catalog());
      setError('');
    } catch (failure) {
      setError((failure as Error).message);
    }
  };
  useEffect(() => {
    let active = true;
    void toolsApi
      .catalog()
      .then((value) => {
        if (active) setCatalog(value);
      })
      .catch((failure) => {
        if (active) setError(failure.message);
      });
    return () => {
      active = false;
    };
  }, []);
  const toggle = async (tool: ManagedTool) => {
    if (!catalog || tool.required) return;
    setNotice('');
    if (!tool.enabled && !tool.ready) {
      setSelected({ tool, enable: true });
      return;
    }
    setBusy(tool.id);
    setError('');
    try {
      setCatalog(
        await toolsApi.save(tool.id, { revision: catalog.revision, enabled: !tool.enabled }),
      );
      setNotice(`${tool.label}已${tool.enabled ? '关闭' : '开启'}`);
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === 'TOOL_CREDENTIAL_REQUIRED')
        setSelected({ tool, enable: true });
      else setError((failure as Error).message);
    } finally {
      setBusy('');
    }
  };
  const items =
    catalog?.items.filter((tool) => `${tool.label} ${tool.description}`.includes(query.trim())) ??
    [];
  return (
    <section className="management-page tools-page">
      <div className="page-heading">
        <div>
          <h2>工具</h2>
          <p>管理工具开关和服务配置。</p>
        </div>
      </div>
      <div className="tool-toolbar">
        <label className="tool-search">
          <Search size={16} />
          <input
            aria-label="搜索工具"
            placeholder="搜索工具"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <span>更改从下一次任务生效</span>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button className="text-button" onClick={() => void load()}>
            重新加载
          </button>
        </div>
      )}
      {notice && (
        <p className="tool-save-status" role="status">
          {notice}
        </p>
      )}
      {!catalog && !error && <p className="list-empty">正在读取工具…</p>}
      {[false, true].map((required) => {
        const group = items.filter((tool) => tool.required === required);
        return (
          group.length > 0 && (
            <section className="settings-section" key={String(required)}>
              <h3>
                {required ? '基础能力' : '可选工具'}
                <span className="section-count">{group.length}</span>
              </h3>
              <div className="setting-rows">
                {group.map((tool) => (
                  <div className="tool-row" key={tool.id}>
                    <button
                      className="tool-row-main"
                      onClick={() => {
                        setSelected({ tool, enable: false });
                        setError('');
                      }}
                    >
                      <ToolIcon tool={tool} />
                      <span>
                        <strong>{tool.label}</strong>
                        <small>{tool.description}</small>
                      </span>
                      <ChevronRight size={16} />
                    </button>
                    <span className={`tool-state ${tool.required ? 'required' : ''}`}>
                      {tool.required
                        ? '始终开启'
                        : !tool.enabled
                          ? '已关闭'
                          : tool.ready
                            ? '已开启'
                            : '待配置'}
                    </span>
                    <input
                      type="checkbox"
                      role="switch"
                      className="tool-switch"
                      aria-label={`${tool.label}开关`}
                      checked={tool.enabled}
                      disabled={tool.required || !!busy}
                      title={tool.required ? 'M-Claw 基础能力，始终开启' : undefined}
                      onChange={() => void toggle(tool)}
                    />
                  </div>
                ))}
              </div>
            </section>
          )
        );
      })}
      {catalog && !items.length && <p className="list-empty">没有找到匹配的工具</p>}
      {selected && catalog && (
        <ToolEditor
          key={selected.tool.id}
          tool={selected.tool}
          revision={catalog.revision}
          enable={selected.enable}
          onClose={() => setSelected(null)}
          onReload={() => {
            setSelected(null);
            void load();
          }}
          onSaved={(value) => {
            setCatalog(value);
            setNotice('工具设置已保存');
          }}
        />
      )}
    </section>
  );
}
