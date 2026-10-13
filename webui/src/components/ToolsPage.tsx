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
  const [mode, setMode] = useState<'auto' | 'main' | 'auxiliary'>(tool.mode ?? 'auxiliary');
  const [baseUrl, setBaseUrl] = useState(tool.base_url ?? '');
  const [imageInput, setImageInput] = useState(tool.image_input ?? 'auto');
  const [mainImageInput, setMainImageInput] = useState(tool.main_image_input ?? 'auto');
  const [mainCapabilityEdited, setMainCapabilityEdited] = useState(false);
  const [catalogProvider, setCatalogProvider] = useState(tool.catalog_provider ?? '');
  const [catalogModel, setCatalogModel] = useState(tool.catalog_model ?? '');
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
    (tool.id !== 'vision' || mode === 'auxiliary') &&
    !(
      tool.id === 'vision' &&
      backend.startsWith('custom') &&
      /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?=[:/]|$)/i.test(baseUrl)
    ) &&
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
      if (tool.id === 'vision') {
        form.mode = mode;
        if (mode !== 'auxiliary' && mainCapabilityEdited) form.main_image_input = mainImageInput;
        if (mode !== 'main') {
          form.base_url = baseUrl;
          form.image_input = imageInput;
          form.catalog_provider = catalogProvider;
          form.catalog_model = catalogModel;
        }
      }
      if (
        tool.backends.length &&
        (enabled || !missingCredential) &&
        (tool.id !== 'vision' || mode !== 'main')
      ) {
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
        {tool.id === 'vision' && (
          <div className="tool-form-fields">
            <label className="field-label">
              视觉模式
              <Select
                label="视觉模式"
                value={mode}
                disabled={busy}
                onChange={(value) => setMode(value as typeof mode)}
              >
                <option value="auto">自动：优先当前模型</option>
                <option value="main">当前模型</option>
                <option value="auxiliary">专用视觉模型</option>
              </Select>
            </label>
            {mode !== 'auxiliary' && (
              <>
                <p className="field-hint">
                  默认模型预览：{tool.main_model || '尚未配置'} · 图片输入：
                  {tool.main_capability?.state === 'supported'
                    ? '支持'
                    : tool.main_capability?.state === 'unsupported'
                      ? '不支持'
                      : '未知'}
                  {tool.main_capability?.source === 'models.dev' ? '（models.dev 声明）' : ''}
                </p>
                <details className="plain-details">
                  <summary>默认模型能力设置（高级）</summary>
                  <label className="field-label">
                    默认模型图片能力
                    <Select
                      label="默认模型图片能力"
                      value={mainImageInput}
                      disabled={busy}
                      onChange={(value) => {
                        setMainImageInput(value);
                        setMainCapabilityEdited(true);
                      }}
                    >
                      <option value="auto">按 models.dev 判断</option>
                      <option value="enabled">手动声明支持图片</option>
                      <option value="disabled">禁用图片输入</option>
                    </Select>
                  </label>
                  <p className="field-hint">
                    仅修改上方默认模型及其接口的能力判断。已有对话以顶部实际选择为准；手动声明不会增加模型本身的能力。
                  </p>
                </details>
              </>
            )}
            {mode === 'auto' && (
              <p className="field-hint">
                当前模型支持图片时直接看图；不支持时使用下方专用视觉模型。能力未知时优先使用已配置的专用视觉模型，否则尝试当前模型。
              </p>
            )}
            {mode !== 'main' && (
              <p className="field-hint">
                专用视觉模型先分析图片，再把分析文字交给当前模型回答。它可以与当前模型使用同一供应商。
              </p>
            )}
          </div>
        )}
        {tool.backends.length > 0 && (tool.id !== 'vision' || mode !== 'main') && (
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
                  if (tool.id === 'vision') {
                    setModel(selectedValue.startsWith('dashscope') ? 'qwen3-vl-flash' : '');
                    setBaseUrl('');
                    setImageInput('auto');
                    setCatalogProvider('');
                    setCatalogModel('');
                  }
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
                  placeholder={tool.id === 'vision' ? '填写专用视觉模型名称' : '使用默认模型'}
                  onChange={(event) => {
                    setModel(event.target.value);
                    if (tool.id === 'vision') {
                      setImageInput('auto');
                      setCatalogProvider('');
                      setCatalogModel('');
                    }
                  }}
                />
              </label>
            )}
            {tool.id === 'vision' && (
              <>
                <label className="field-label">
                  接口地址
                  <input
                    value={baseUrl}
                    disabled={busy}
                    placeholder="留空使用供应商默认地址"
                    onChange={(event) => {
                      setBaseUrl(event.target.value);
                      setImageInput('auto');
                      setCatalogProvider('');
                      setCatalogModel('');
                    }}
                  />
                </label>
                <details className="plain-details">
                  <summary>专用视觉模型能力设置（高级）</summary>
                  <label className="field-label">
                    专用视觉模型图片能力
                    <Select
                      label="专用视觉模型图片能力"
                      value={imageInput}
                      disabled={busy}
                      onChange={setImageInput}
                    >
                      <option value="auto">按 models.dev 判断</option>
                      <option value="enabled">手动声明支持图片</option>
                      <option value="disabled">禁用图片输入</option>
                    </Select>
                  </label>
                  <label className="field-label">
                    models.dev 供应商映射
                    <input
                      value={catalogProvider}
                      disabled={busy}
                      placeholder="可选，用于自定义接口"
                      onChange={(event) => setCatalogProvider(event.target.value)}
                    />
                  </label>
                  <label className="field-label">
                    models.dev 模型映射
                    <input
                      value={catalogModel}
                      disabled={busy}
                      placeholder="可选，用于模型别名"
                      onChange={(event) => setCatalogModel(event.target.value)}
                    />
                  </label>
                </details>
              </>
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
