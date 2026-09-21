import { Select } from './Select';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  ArrowLeft,
  Check,
  ChevronRight,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  X,
} from 'lucide-react';
import { providersApi } from '../api';
import type { ProviderCandidate, ProviderCatalog, ProviderForm, ProviderProfile } from '../types';
import { ProviderLogo } from './ProviderLogo';
import { ModelLibrary } from './ModelLibrary';

export function Providers({
  onClose,
  onUpdated,
  refreshOnOpen = false,
}: {
  onClose: () => void;
  onUpdated: () => Promise<void>;
  refreshOnOpen?: boolean;
}) {
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [view, setView] = useState<'list' | 'choose' | 'form' | 'library'>(
    refreshOnOpen ? 'library' : 'list',
  );
  const [query, setQuery] = useState('');
  const [candidate, setCandidate] = useState<ProviderCandidate | null>(null);
  const [form, setForm] = useState<ProviderForm | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const refreshRequest = useRef<AbortController | null>(null);
  const refreshPending = useRef(refreshOnOpen);
  const listRequest = useRef<AbortController | null>(null);
  const probeRequest = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const formOrigin = useRef<'list' | 'choose'>('choose');
  const panel = useRef<HTMLElement>(null);
  const title = useId();
  const profile = candidate?.profiles.find((item) => item.id === form?.profile);
  const isDefault =
    catalog?.configured.some(
      (item) => item.runtime_provider === profile?.runtime_provider && item.is_default,
    ) ?? false;

  const load = useCallback(async () => {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setError('');
    try {
      const next = await providersApi.list(controller.signal);
      if (!controller.signal.aborted) {
        setCatalog(next);
        if (!next.configured.length)
          setView((current) => (current === 'list' ? 'choose' : current));
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '读取供应商失败');
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    void load();
    return () => {
      mounted.current = false;
      listRequest.current?.abort();
      probeRequest.current?.abort();
      refreshRequest.current?.abort();
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [load]);
  useEffect(() => {
    panel.current?.querySelector<HTMLElement>('[data-autofocus]')?.focus();
  }, [view, !!catalog]);
  useEffect(() => {
    if (!catalog || !candidate) return;
    const next = catalog.catalog.find((item) => item.id === candidate.id);
    if (next && next !== candidate) {
      setCandidate(next);
      setModels(next.profiles.find((item) => item.id === form?.profile)?.models ?? []);
    }
  }, [catalog, candidate, form?.profile]);
  useEffect(() => {
    if (!catalog?.library?.refreshing || refreshing) return;
    const timer = setTimeout(() => void load(), 1000);
    return () => clearTimeout(timer);
  }, [catalog?.library, refreshing, load]);

  const refreshLibrary = async () => {
    if (refreshRequest.current && !refreshRequest.current.signal.aborted) return;
    const controller = new AbortController();
    refreshRequest.current = controller;
    setRefreshing(true);
    setError('');
    setNotice('');
    try {
      const status = await providersApi.refreshLibrary(controller.signal);
      if (controller.signal.aborted) return;
      await load();
      if (controller.signal.aborted) return;
      if (!status.last_refresh?.ok) setError('模型库刷新失败，原有列表已保留，请稍后重试。');
      else {
        setNotice(`模型库已更新，共 ${status.models.toLocaleString()} 个模型`);
        await onUpdated();
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '刷新模型库失败');
    } finally {
      if (!controller.signal.aborted) {
        setRefreshing(false);
        refreshRequest.current = null;
      }
    }
  };
  useEffect(() => {
    if (catalog && refreshPending.current) {
      refreshPending.current = false;
      void refreshLibrary();
    }
  }, [catalog]);

  const cancelProbe = () => {
    probeRequest.current?.abort();
    probeRequest.current = null;
    setProbing(false);
  };
  const pickProfile = (provider: ProviderCandidate, next: ProviderProfile) => {
    if (view === 'list' || view === 'choose') formOrigin.current = view;
    cancelProbe();
    setError('');
    setNotice('');
    setCandidate(provider);
    setModels(next.models);
    setForm({
      provider: provider.id,
      profile: next.id,
      api_key: '',
      base_url: next.base_url,
      model: next.model,
      make_default: false,
      revision: catalog!.revision,
    });
    setView('form');
  };
  const back = () => {
    cancelProbe();
    setForm(null);
    setCandidate(null);
    setError('');
    setNotice('');
    setView(view === 'form' ? formOrigin.current : 'list');
  };
  const edit = (patch: Partial<ProviderForm>) => {
    cancelProbe();
    setError('');
    setNotice('');
    setForm((current) => (current ? { ...current, ...patch } : null));
  };
  const probe = async () => {
    if (!form) return;
    cancelProbe();
    const controller = new AbortController();
    probeRequest.current = controller;
    setProbing(true);
    setError('');
    setNotice('');
    try {
      const result = await providersApi.models(form, controller.signal);
      if (!controller.signal.aborted) {
        setModels(result.models);
        setNotice(
          result.models.length
            ? `找到 ${result.models.length} 个模型`
            : '接口没有返回模型列表，可以直接填写名称。',
        );
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '获取模型失败');
    } finally {
      if (!controller.signal.aborted) setProbing(false);
    }
  };
  const save = async () => {
    if (!form || saving) return;
    cancelProbe();
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const next = await providersApi.save(form);
      if (!mounted.current) return;
      setCatalog(next);
      setForm(null);
      setCandidate(null);
      setQuery('');
      setView('list');
      setNotice('已保存');
      try {
        await onUpdated();
      } catch {
        if (mounted.current) setError('供应商已保存，模型列表暂未刷新。重新打开页面后即可使用。');
      }
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : '保存失败，请重试');
    } finally {
      if (mounted.current) setSaving(false);
    }
  };
  const filtered =
    catalog?.catalog.filter((item) =>
      `${item.label} ${item.keywords}`
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
    ) ?? [];

  return (
    <div
      className="modal-backdrop provider-backdrop"
      onClick={() => {
        if (!saving) onClose();
      }}
    >
      <section
        ref={panel}
        className="provider-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={title}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.stopPropagation();
            if (!saving) onClose();
          }
          if (event.key === 'Tab') {
            const items = panel.current?.querySelectorAll<HTMLElement>(
              'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]',
            );
            if (!items?.length) return;
            const first = items[0],
              last = items[items.length - 1];
            if (
              event.shiftKey &&
              (document.activeElement === first ||
                document.activeElement?.getAttribute('tabindex') === '-1')
            ) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }
        }}
      >
        <header className="provider-heading">
          {view !== 'list' && (
            <button
              type="button"
              className="icon-button"
              aria-label="返回供应商列表"
              disabled={saving}
              onClick={back}
            >
              <ArrowLeft size={18} />
            </button>
          )}
          <h2 id={title} tabIndex={-1} data-autofocus={view !== 'choose' ? '' : undefined}>
            {view === 'list'
              ? '供应商与模型'
              : view === 'choose'
                ? '新增供应商'
                : view === 'library'
                  ? '模型库'
                  : candidate?.label}
          </h2>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭供应商设置"
            disabled={saving}
            onClick={onClose}
          >
            <X size={19} />
          </button>
        </header>
        {catalog && view !== 'form' && (
          <div className="provider-library-bar">
            <div>
              {catalog.library?.updated_at ? (
                <span>
                  更新于{' '}
                  {new Date(catalog.library.updated_at * 1000).toLocaleString('zh-CN', {
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              ) : (
                <span>模型库尚未更新</span>
              )}
              {view !== 'library' && (
                <button
                  className="text-button"
                  onClick={() => {
                    setView('library');
                    setError('');
                    setNotice('');
                  }}
                >
                  查看模型库
                </button>
              )}
            </div>
            <button
              className="secondary"
              disabled={refreshing || catalog.library?.refreshing}
              onClick={() => void refreshLibrary()}
            >
              <RefreshCw
                size={15}
                className={refreshing || catalog.library?.refreshing ? 'spin' : ''}
              />
              {refreshing || catalog.library?.refreshing ? '正在刷新…' : '刷新模型库'}
            </button>
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            {error}
          </div>
        )}
        {notice && (
          <p className="provider-notice" role="status">
            <Check size={15} />
            {notice}
          </p>
        )}
        {!catalog &&
          (error ? (
            <button className="text-button" onClick={() => void load()}>
              重新加载
            </button>
          ) : (
            <p className="provider-loading" role="status">
              正在读取供应商…
            </p>
          ))}
        {catalog && view === 'library' && <ModelLibrary updatedAt={catalog.library?.updated_at} />}
        {catalog && view === 'list' && (
          <>
            <div className="provider-list-bar">
              <span>已添加 {catalog.configured.length} 个</span>
              <button
                className="primary"
                onClick={() => {
                  setView('choose');
                  setError('');
                  setNotice('');
                }}
              >
                <Plus size={16} />
                新增供应商
              </button>
            </div>
            <div className="provider-configured">
              {catalog.configured.map((item) => (
                <button
                  className="provider-saved"
                  key={`${item.provider}:${item.profile}`}
                  onClick={() => {
                    const selected = catalog.catalog.find(({ id }) => id === item.provider);
                    const selectedProfile = selected?.profiles.find(
                      ({ id }) => id === item.profile,
                    );
                    if (selected && selectedProfile) pickProfile(selected, selectedProfile);
                  }}
                >
                  <ProviderLogo provider={item.provider} />
                  <span className="provider-copy">
                    <strong>
                      {item.label}
                      {item.is_default && <span className="provider-default">默认</span>}
                    </strong>
                    <small>
                      {item.region} · {item.model || '尚未选择模型'}
                    </small>
                  </span>
                  <ChevronRight size={17} />
                </button>
              ))}
              {!catalog.configured.length && (
                <p className="muted">添加一个供应商，就可以选择它提供的模型。</p>
              )}
            </div>
          </>
        )}
        {catalog && view === 'choose' && (
          <>
            <label className="provider-search">
              <Search size={18} aria-hidden="true" />
              <input
                data-autofocus
                aria-label="搜索供应商"
                placeholder="搜索供应商，如 Qwen、DeepSeek"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <div className="provider-candidates">
              {filtered.map((item) => (
                <button
                  key={item.id}
                  className="provider-candidate"
                  onClick={() => {
                    const previous =
                      catalog.configured.find(
                        (saved) => saved.provider === item.id && saved.is_default,
                      ) ?? catalog.configured.find((saved) => saved.provider === item.id);
                    pickProfile(
                      item,
                      item.profiles.find((profile) => profile.id === previous?.profile) ??
                        item.profiles[0],
                    );
                  }}
                >
                  <ProviderLogo provider={item.id} />
                  <span>{item.label}</span>
                  {catalog.configured.some((saved) => saved.provider === item.id) && (
                    <small>已添加</small>
                  )}
                </button>
              ))}
            </div>
            {!filtered.length && (
              <div className="provider-empty">
                <p>没有找到“{query}”</p>
                <button className="text-button" onClick={() => setQuery('自定义')}>
                  使用自定义兼容接口
                </button>
              </div>
            )}
          </>
        )}
        {view === 'form' && form && profile && candidate && (
          <form
            className="provider-form"
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <fieldset disabled={saving}>
              {candidate.profiles.length > 1 && (
                <label className="field-label">
                  API 区域与接口
                  <Select
                    label="API 区域与接口"
                    disabled={saving}
                    value={form.profile}
                    onChange={(selectedValue) =>
                      pickProfile(
                        candidate,
                        candidate.profiles.find(({ id }) => id === selectedValue)!,
                      )
                    }
                  >
                    {candidate.profiles.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </Select>
                </label>
              )}
              <label className="field-label">
                服务地址
                <input
                  type="url"
                  required
                  value={form.base_url}
                  readOnly={!profile.base_url_editable}
                  onChange={(event) => edit({ base_url: event.target.value })}
                  placeholder="https://example.com/v1"
                />
              </label>
              <label className="field-label">
                API Key
                <input
                  type="password"
                  autoComplete="new-password"
                  value={form.api_key}
                  onChange={(event) => edit({ api_key: event.target.value })}
                  placeholder={
                    profile.credential_configured ? '已保存，留空沿用' : '输入此接口的 API Key'
                  }
                />
              </label>
              {profile.key_url && (
                <a
                  className="provider-key-link"
                  href={profile.key_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  获取 API Key
                </a>
              )}
              <div className="provider-model-field">
                <label className="field-label">
                  模型
                  <Select
                    label="模型"
                    editable
                    disabled={saving}
                    required
                    value={form.model}
                    onChange={(value) => edit({ model: value })}
                    placeholder="选择或填写模型名称"
                  >
                    {models.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </Select>
                </label>
                <button
                  type="button"
                  className="secondary"
                  disabled={probing}
                  onClick={() => void probe()}
                >
                  {probing && <LoaderCircle size={15} className="spin" />}
                  {probing ? '正在获取…' : '获取模型'}
                </button>
              </div>
              <p className="provider-hint">
                模型名称以供应商提供的 API 文档为准，也可以点击“获取模型”读取可用列表。
              </p>
              <label className="provider-default-choice">
                <input
                  type="checkbox"
                  checked={isDefault || form.make_default}
                  disabled={isDefault}
                  onChange={(event) => edit({ make_default: event.target.checked })}
                />
                {isDefault ? '当前默认模型' : '设为新会话的默认模型'}
              </label>
            </fieldset>
            <footer className="provider-form-actions">
              <button type="button" className="text-button" disabled={saving} onClick={back}>
                取消
              </button>
              <button type="submit" className="primary" disabled={saving || !form.model.trim()}>
                {saving && <LoaderCircle size={16} className="spin" />}
                {saving ? '正在保存…' : '保存'}
              </button>
            </footer>
          </form>
        )}
      </section>
    </div>
  );
}
