import { Select } from './Select';
import { useCallback, useEffect, useState } from 'react';
import { ChevronRight, LoaderCircle, RefreshCw, X } from 'lucide-react';
import { request } from '../api';
import { ChannelLogo } from './ChannelLogo';

type ChannelId = 'weixin' | 'dingtalk';
type Field = {
  name: string;
  label: string;
  kind: 'boolean' | 'number' | 'text' | 'list' | 'json' | 'select';
  options: string[];
  advanced: boolean;
};
type Channel = {
  id: ChannelId;
  label: string;
  enabled: boolean;
  configured: boolean;
  status: string;
  error: string;
  settings: Record<string, unknown>;
  credentials: Record<string, boolean>;
  fields: Field[];
  validation_errors: string[];
};
type Login = { id: string; status: string; qr_code: string | null; message: string; error: string };
type Diagnostic = {
  ready: boolean;
  requirements: Record<string, boolean>;
  errors: string[];
  provider: string;
  model: string;
};
const stateLabels: Record<string, string> = {
  stopped: '已停止',
  starting: '正在启动',
  running: '运行中',
  stopping: '正在停止',
  error: '连接异常',
};
const optionLabels: Record<string, string> = {
  open: '允许响应',
  allowlist: '仅允许列表',
  disabled: '关闭响应',
  mention_only: '仅被提及时响应',
  chat: '按聊天',
  user: '按用户',
  chat_user: '按聊天与用户',
};
const credentialLabels: Record<string, string> = {
  account_id: '微信账号标识',
  token: '微信登录凭据',
  client_id: 'Client ID',
  client_secret: 'Client Secret',
  robot_code: 'Robot Code',
};

export function ChannelsPage({ notify }: { notify?: (message: string) => void } = {}) {
  const [items, setItems] = useState<Channel[]>([]);
  const [selected, setSelected] = useState<Channel | null>(null);
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [login, setLogin] = useState<Login | null>(null);
  const [diagnostic, setDiagnostic] = useState<Diagnostic | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setItems((await request<{ items: Channel[] }>('/channels')).items);
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [load]);
  useEffect(() => {
    if (!login || !['running', 'waiting'].includes(login.status)) return;
    let disposed = false;
    const timer = window.setInterval(async () => {
      try {
        const value = await request<Login>(`/channels/weixin/login/${login.id}`);
        if (disposed) return;
        setLogin(value);
        if (value.status === 'completed') {
          setNotice('微信登录信息已保存，现在可以连接微信。');
          void load();
        }
      } catch (failure) {
        if (!disposed) setError((failure as Error).message);
      }
    }, 1000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [login?.id, login?.status, load]);

  const perform = async (operation: () => Promise<unknown>, message = '') => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await operation();
      if (message) {
        setNotice(message);
        notify?.(message);
      }
      await load();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const edit = (item: Channel) => {
    setSelected(item);
    setSecrets({});
    setDiagnostic(null);
    setError('');
    setSettings(
      Object.fromEntries(
        item.fields.map((field) => [
          field.name,
          field.kind === 'list'
            ? (Array.isArray(item.settings[field.name])
                ? (item.settings[field.name] as unknown[])
                : []
              ).join('\n')
            : field.kind === 'json'
              ? JSON.stringify(item.settings[field.name], null, 2)
              : item.settings[field.name],
        ]),
      ),
    );
  };
  const save = () =>
    perform(async () => {
      if (!selected) return;
      const values = Object.fromEntries(
        selected.fields.map((field) => {
          const value = settings[field.name];
          return [
            field.name,
            field.kind === 'list'
              ? String(value ?? '')
                  .split(/[\n,]/)
                  .map((part) => part.trim())
                  .filter(Boolean)
              : field.kind === 'json'
                ? JSON.parse(String(value || '{}'))
                : field.kind === 'number'
                  ? Number(value)
                  : value,
          ];
        }),
      );
      const updated = await request<Channel>(`/channels/${selected.id}`, 'PATCH', {
        settings: values,
        credentials: secrets,
      });
      edit(updated);
    }, '渠道设置已保存。');
  const field = (value: Field) => (
    <label className="field-label" key={value.name}>
      <span>{value.label}</span>
      {value.kind === 'boolean' ? (
        <input
          type="checkbox"
          checked={Boolean(settings[value.name])}
          onChange={(event) => setSettings({ ...settings, [value.name]: event.target.checked })}
        />
      ) : value.kind === 'select' ? (
        <Select
          label={value.label}
          value={String(settings[value.name] ?? '')}
          onChange={(selectedValue) => setSettings({ ...settings, [value.name]: selectedValue })}
        >
          {value.options.map((option) => (
            <option key={option} value={option}>
              {optionLabels[option] ?? option}
            </option>
          ))}
        </Select>
      ) : ['list', 'json'].includes(value.kind) ? (
        <textarea
          rows={value.kind === 'json' ? 5 : 3}
          value={String(settings[value.name] ?? '')}
          placeholder={value.kind === 'list' ? '每行一项' : ''}
          onChange={(event) => setSettings({ ...settings, [value.name]: event.target.value })}
        />
      ) : (
        <input
          type={value.kind === 'number' ? 'number' : 'text'}
          min={value.kind === 'number' ? 0 : undefined}
          step="any"
          value={String(settings[value.name] ?? '')}
          onChange={(event) => setSettings({ ...settings, [value.name]: event.target.value })}
        />
      )}
    </label>
  );
  return (
    <section className="channel-settings settings-section">
      <div className="page-heading">
        <div>
          <h3>消息渠道</h3>
        </div>
        <button className="secondary" onClick={() => void load()} aria-label="刷新渠道状态">
          <RefreshCw size={16} />
        </button>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      <div>
        <div className="setting-rows">
          {items.map((item) => (
            <div className="setting-row" key={item.id}>
              <div className="channel-identity">
                <ChannelLogo source={item.id} size={28} />
                <div>
                  <strong>{item.label}</strong>
                  <small>
                    {stateLabels[item.status] ?? item.status} ·{' '}
                    {item.configured ? '已配置' : '待配置'}
                    {item.enabled ? ' · 随服务启动' : ''}
                  </small>
                  {item.error && <small role="alert">{item.error}</small>}
                </div>
              </div>
              <div className="row-actions">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => {
                    if (!item.configured && !['running', 'starting'].includes(item.status)) {
                      edit(item);
                      return;
                    }
                    void perform(() =>
                      request(
                        `/channels/${item.id}/${['running', 'starting'].includes(item.status) ? 'disconnect' : 'connect'}`,
                        'POST',
                      ),
                    );
                  }}
                >
                  {['running', 'starting'].includes(item.status) ? '断开' : '连接'}
                </button>
                <button className="secondary" onClick={() => edit(item)}>
                  设置
                  <ChevronRight size={16} />
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
      {selected && (
        <div className="modal-backdrop">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label={`${selected.label}渠道设置`}
          >
            <div className="page-heading">
              <h2 className="channel-identity">
                <ChannelLogo source={selected.id} size={24} />
                {selected.label}渠道设置
              </h2>
              <button
                className="icon-button"
                aria-label="关闭渠道设置"
                onClick={() => {
                  setSelected(null);
                  setSecrets({});
                }}
              >
                <X size={18} />
              </button>
            </div>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            {selected.id === 'weixin' && (
              <div className="setting-row">
                <div>
                  <strong>微信扫码登录</strong>
                  <small>扫码并确认后保存登录信息，用于连接微信私聊。</small>
                </div>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void perform(async () =>
                      setLogin(await request<Login>('/channels/weixin/login', 'POST')),
                    )
                  }
                >
                  扫码登录
                </button>
              </div>
            )}
            <details className="plain-details" open={selected.id === 'dingtalk'}>
              <summary>接入凭据</summary>
              <p>已保存的凭据留空即可。</p>
              <div className="form-grid">
                {Object.keys(selected.credentials).map((key) => (
                  <label className="field-label" key={key}>
                    <span>
                      {credentialLabels[key] ?? key}
                      {selected.credentials[key] ? ' · 已保存' : ' · 待填写'}
                    </span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={secrets[key] ?? ''}
                      onChange={(event) => setSecrets({ ...secrets, [key]: event.target.value })}
                      placeholder={selected.credentials[key] ? '留空复用' : '填写凭据'}
                    />
                  </label>
                ))}
              </div>
            </details>
            <div className="form-grid">
              {selected.fields.filter((value) => !value.advanced).map(field)}
            </div>
            <details className="plain-details">
              <summary>高级设置</summary>
              <div className="form-grid">
                {selected.fields.filter((value) => value.advanced).map(field)}
              </div>
            </details>
            <p className="muted">
              保存会重新应用连接配置。正在执行的渠道任务会停止并保留，可在会话中核对后继续。
            </p>
            {diagnostic && (
              <div className={diagnostic.ready ? 'notice' : 'error-banner'} role="status">
                <strong>{diagnostic.ready ? '诊断通过' : '还需处理'}</strong>
                {diagnostic.errors.map((value) => (
                  <p key={value}>{value}</p>
                ))}
                {diagnostic.model && <p>模型：{diagnostic.model}</p>}
              </div>
            )}
            <div className="form-actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  void perform(async () =>
                    setDiagnostic(
                      await request<Diagnostic>(`/channels/${selected.id}/diagnose`, 'POST'),
                    ),
                  )
                }
              >
                运行诊断
              </button>
              <button className="primary" disabled={busy} onClick={() => void save()}>
                {busy && <LoaderCircle size={16} className="spin" />}保存设置
              </button>
            </div>
          </section>
        </div>
      )}
      {login && (
        <div className="modal-backdrop">
          <section className="modal" role="dialog" aria-modal="true" aria-label="微信扫码登录">
            <div className="page-heading">
              <h2>微信扫码登录</h2>
              <button
                className="icon-button"
                aria-label="关闭扫码登录"
                onClick={() => {
                  if (['waiting', 'running'].includes(login.status))
                    void perform(() => request(`/channels/weixin/login/${login.id}`, 'DELETE'));
                  setLogin(null);
                }}
              >
                <X size={18} />
              </button>
            </div>
            {login.qr_code && (
              <img src={login.qr_code} width={240} height={240} alt="微信登录二维码" />
            )}
            <p role="status">{login.message}</p>
            {login.error && <p className="error-banner">{login.error}</p>}
            {login.status === 'completed' && (
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    await request('/channels/weixin/connect', 'POST');
                    setLogin(null);
                  })
                }
              >
                连接微信
              </button>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
