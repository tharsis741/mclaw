import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowRight,
  AudioLines,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronRight,
  Cpu,
  Eye,
  FileText,
  FolderOpen,
  Globe2,
  KeyRound,
  Link,
  LoaderCircle,
  MessageCircle,
  MessageSquare,
  Mic,
  Monitor,
  MousePointer2,
  Network,
  Plug,
  X,
  Search,
  type LucideIcon,
} from 'lucide-react';
import { api } from '../api';
import type { SetupPrompt, SetupRun, SetupStatus } from '../types';
import { ProviderLogo } from './ProviderLogo';
import { BrandLogo } from './BrandLogo';

const CAPABILITY_ICONS: Record<string, LucideIcon> = {
  web_extract: FileText,
  web_search: Search,
  vision: Eye,
  browser: MousePointer2,
  dsoftbus: Network,
  weixin: MessageCircle,
  dingtalk: MessageSquare,
  inbound_audio: AudioLines,
  asr: Mic,
  trafilatura: FileText,
  tavily: Search,
  firecrawl: Globe2,
  dashscope: Search,
};
const STEPS = ['连接大模型', '选择工具', '消息与语音', '添加技能'];
const STEP_BY_VIEW: Record<string, number> = {
  welcome: 0,
  providers: 0,
  profiles: 0,
  models: 0,
  capabilities: 1,
  channels: 2,
  voice: 2,
  skills: 3,
};

function OptionIcon({ prompt, id }: { prompt: SetupPrompt; id: string }) {
  if (prompt.presentation === 'providers') return <ProviderLogo provider={id} />;
  const Icon =
    prompt.presentation === 'profiles'
      ? Globe2
      : prompt.presentation === 'models'
        ? Cpu
        : prompt.presentation === 'skills'
          ? BookOpen
          : (CAPABILITY_ICONS[id] ?? ChevronRight);
  return (
    <span className="setup-option-icon" aria-hidden="true">
      <Icon size={20} />
    </span>
  );
}

export function Setup({
  onComplete,
  onClose,
  active = true,
}: {
  onComplete: () => void;
  onClose?: () => void;
  active?: boolean;
}) {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [run, setRun] = useState<SetupRun | null>(null);
  const [prompt, setPrompt] = useState<SetupPrompt | null>(null);
  const [step, setStep] = useState(0);
  const [value, setValue] = useState<string | string[]>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const closing = useRef(false);
  const promptId = useRef<string | null>(null);
  const formHeading = useRef<HTMLHeadingElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const accept = useCallback((next: SetupRun) => {
    setRun(next);
    // Keep the form in place while a probe or installation is running.
    if (next.prompt || next.status !== 'running' || next.qr_code) {
      setPrompt(next.prompt);
      if (next.prompt?.id !== promptId.current) {
        promptId.current = next.prompt?.id ?? null;
        const form = next.prompt;
        setValue(
          !form || form.kind === 'secret'
            ? ''
            : form.kind === 'single_select' && Array.isArray(form.default)
              ? (form.default[0] ?? '')
              : (form.default ?? (form.kind === 'multi_select' ? [] : '')),
        );
      }
    }
    if (next.prompt?.presentation && next.prompt.presentation in STEP_BY_VIEW)
      setStep(STEP_BY_VIEW[next.prompt.presentation]);
  }, []);
  useEffect(() => {
    let disposed = false;
    void api
      .setupStatus()
      .then(async (next) => {
        if (disposed) return;
        setStatus(next);
        const current = next.active_run_id
          ? await api.setupRun(next.active_run_id)
          : await api.setupStart();
        if (!disposed) accept(current);
        else if (!next.active_run_id || closing.current)
          void api.setupCancel(current.id).catch(() => {});
      })
      .catch((e) => {
        if (!disposed) setError(e.message);
      });
    return () => {
      disposed = true;
    };
  }, [accept]);
  useEffect(() => {
    if (!prompt || !active) return;
    if (prompt.kind === 'text' || prompt.kind === 'secret') input.current?.focus();
    else formHeading.current?.focus();
  }, [prompt?.id, active]);
  useEffect(() => {
    if (!run || run.status !== 'running' || busy) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const currentGeneration = generation.current;
    const poll = async () => {
      try {
        const next = await api.setupRun(run.id);
        if (disposed || generation.current !== currentGeneration) return;
        accept(next);
        setError('');
        if (next.status !== 'running') return;
      } catch (e) {
        if (disposed || generation.current !== currentGeneration) return;
        setError(e instanceof Error ? e.message : '连接中断，正在重试…');
      }
      if (!disposed) timer = setTimeout(() => void poll(), 300);
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [run?.id, run?.status, busy, accept]);
  const act = async (action: () => Promise<SetupRun>) => {
    if (busy) return;
    const currentGeneration = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const next = await action();
      if (generation.current === currentGeneration) accept(next);
    } catch (e) {
      if (generation.current !== currentGeneration) return;
      setError(e instanceof Error ? e.message : '操作失败，请重试。');
      // A response may be lost after the backend accepted the answer.
      if (run) {
        try {
          accept(await api.setupRun(run.id));
        } catch {
          /* Keep the entered value for retry. */
        }
      }
    } finally {
      if (generation.current === currentGeneration) setBusy(false);
    }
  };
  const working = busy || run?.status === 'running';
  const close = async () => {
    closing.current = true;
    ++generation.current;
    setBusy(true);
    setError('');
    try {
      if (run && (run.status === 'running' || run.status === 'waiting'))
        await api.setupCancel(run.id);
      onClose?.();
    } catch (e) {
      closing.current = false;
      setError(e instanceof Error ? e.message : '退出设置失败，请重试。');
      setBusy(false);
    }
  };
  const welcome = prompt?.presentation === 'welcome';
  const multi = prompt?.kind === 'multi_select';
  const options = prompt?.options ?? [];
  const selectedCount = Array.isArray(value)
    ? options.filter((item) => value.includes(item.id)).length
    : 0;
  const allSelected = options.length > 0 && selectedCount === options.length;
  const submit = (answer: string | string[] = value) => {
    if (run && prompt && !working) void act(() => api.setupSubmit(run.id, prompt.id, answer));
  };
  return (
    <main className="setup-shell">
      <div className="setup-brand">
        <BrandLogo />
        <strong>M-Claw</strong>
        <span>设置</span>
        {onClose && (
          <button className="icon-button" aria-label="关闭设置" onClick={() => void close()}>
            <X size={18} />
          </button>
        )}
      </div>
      <div className="setup-layout">
        <aside className="setup-overview">
          <h1>{status?.ready ? '调整你的配置' : '准备开始'}</h1>
          <p>
            {status?.ready
              ? '模型、工具和消息渠道，都可以在这里调整。'
              : '连接模型后，就可以开始对话。工具和消息渠道可以按需选择。'}
          </p>
          <ol>
            {STEPS.map((label, index) => (
              <li key={label} aria-current={step === index ? 'step' : undefined}>
                <span>
                  {index < step || run?.status === 'completed' ? <Check size={13} /> : index + 1}
                </span>
                {label}
              </li>
            ))}
          </ol>
          <div className="setup-local">
            <Monitor size={14} />
            配置保存在本机
          </div>
        </aside>
        <section className="setup-card">
          {error && (
            <div className="error-banner" role="alert">
              {error}
            </div>
          )}
          {!run &&
            (error ? (
              <button className="primary" onClick={() => act(api.setupStart)} disabled={busy}>
                重新打开配置
              </button>
            ) : (
              <p className="muted" role="status">
                正在打开配置…
              </p>
            ))}
          {run?.status === 'completed' && (
            <>
              <CheckCircle2 className="success-icon" size={40} />
              <h2>配置已完成</h2>
              <p className="muted">{run.configuration.model}</p>
              <button className="primary large" onClick={onComplete}>
                开始使用
                <ArrowRight size={17} />
              </button>
            </>
          )}
          {run?.status === 'cancelled' && (
            <>
              <h2>已退出配置</h2>
              <p className="muted">
                {run.saved
                  ? '已保存的配置会保留，下次打开时可以接着修改。'
                  : '需要时可以重新开始。'}
              </p>
              <button className="primary" disabled={busy} onClick={() => act(api.setupStart)}>
                重新开始
              </button>
            </>
          )}
          {run?.status === 'failed' && (
            <>
              <h2>配置尚未完成</h2>
              <div className="error-banner" role="alert">
                {run.error?.message ?? '请重新打开配置，再试一次。'}
              </div>
              <button className="primary" disabled={busy} onClick={() => act(api.setupStart)}>
                重新配置
              </button>
            </>
          )}
          {prompt && (
            <form
              aria-busy={working}
              onSubmit={(event) => {
                event.preventDefault();
                submit(welcome ? 'y' : value);
              }}
            >
              <div className="setup-form-heading">
                {welcome ? (
                  <BrandLogo />
                ) : prompt.provider ? (
                  <ProviderLogo provider={prompt.provider} />
                ) : (
                  <span className="section-icon" aria-hidden="true">
                    {prompt.kind === 'secret' ? (
                      <KeyRound />
                    ) : prompt.presentation === 'skills' ? (
                      <BookOpen />
                    ) : (
                      <Plug />
                    )}
                  </span>
                )}
                <h2 ref={formHeading} tabIndex={-1}>
                  {prompt.title}
                </h2>
              </div>
              {prompt.hint && <p className="muted prompt-hint">{prompt.hint}</p>}
              {welcome ? (
                <div className="setup-disclosure">
                  <div>
                    <FolderOpen size={20} aria-hidden="true" />
                    <div>
                      <h3>你的文件</h3>
                      <p>
                        M-Claw
                        会按你的要求读取和修改文件、运行命令。它使用当前系统账户的权限，也能访问工作目录以外的文件。
                      </p>
                    </div>
                  </div>
                  <div>
                    <MessageSquare size={20} aria-hidden="true" />
                    <div>
                      <h3>模型服务</h3>
                      <p>你的对话，以及完成任务所需的文件内容，可能发送给你选择的模型供应商。</p>
                    </div>
                  </div>
                  <div>
                    <KeyRound size={20} aria-hidden="true" />
                    <div>
                      <h3>API Key</h3>
                      <p>密钥保存在这台电脑上，用于连接相应服务。</p>
                      <small>{prompt.credential_path}</small>
                    </div>
                  </div>
                </div>
              ) : (
                <fieldset className="setup-fields" disabled={working}>
                  {prompt.secret_configured && (
                    <div className="notice">
                      <Check size={15} />
                      已保存凭据，留空即可沿用。
                    </div>
                  )}
                  {prompt.presentation === 'notice' ? null : prompt.kind === 'single_select' ||
                    multi ? (
                    <>
                      {multi && (
                        <div className="setup-selection-toolbar">
                          <span>已选 {selectedCount} 项</span>
                          <button
                            type="button"
                            className="text-button"
                            onClick={() =>
                              setValue(allSelected ? [] : options.map((item) => item.id))
                            }
                          >
                            {allSelected ? '取消全选' : '全选'}
                          </button>
                        </div>
                      )}
                      <div className="setup-options" key={prompt.id}>
                        {options.map((option) => {
                          const selected = Array.isArray(value)
                            ? value.includes(option.id)
                            : value === option.id;
                          return (
                            <label
                              className={`setup-option ${selected ? 'selected' : ''}`}
                              key={option.id}
                            >
                              <input
                                type={multi ? 'checkbox' : 'radio'}
                                name="setup-selection"
                                checked={selected}
                                onChange={() =>
                                  setValue(
                                    multi
                                      ? selected
                                        ? (Array.isArray(value) ? value : []).filter(
                                            (id) => id !== option.id,
                                          )
                                        : [...(Array.isArray(value) ? value : []), option.id]
                                      : option.id,
                                  )
                                }
                              />
                              <OptionIcon prompt={prompt} id={option.id} />
                              <span className="setup-option-copy">
                                <strong>{option.label}</strong>
                                {option.description && <small>{option.description}</small>}
                                {option.category && <small>{option.category}</small>}
                              </span>
                              {selected && <Check size={17} aria-hidden="true" />}
                            </label>
                          );
                        })}
                      </div>
                    </>
                  ) : (
                    <label className="field-label">
                      {prompt.field_label ?? (prompt.kind === 'secret' ? '密钥' : '填写内容')}
                      <input
                        ref={input}
                        key={prompt.id}
                        type={prompt.kind === 'secret' ? 'password' : 'text'}
                        autoComplete={prompt.kind === 'secret' ? 'new-password' : 'off'}
                        placeholder={prompt.placeholder}
                        value={typeof value === 'string' ? value : ''}
                        onChange={(event) => setValue(event.target.value)}
                      />
                    </label>
                  )}
                  {prompt.key_url && (
                    <a
                      className="setup-key-link"
                      href={prompt.key_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <Link size={14} />
                      到供应商网站获取 API Key
                    </a>
                  )}
                </fieldset>
              )}
              <div className="setup-actions">
                <button
                  type="button"
                  className="text-button"
                  disabled={busy}
                  onClick={() => act(() => api.setupCancel(run!.id))}
                >
                  {welcome ? '暂不配置' : '退出配置'}
                </button>
                <button className="primary" disabled={working}>
                  {working && <LoaderCircle size={16} className="spin" />}
                  {welcome ? '同意并开始配置' : '下一步'}
                  {!working && <ArrowRight size={16} />}
                </button>
              </div>
            </form>
          )}
          {run?.qr_code && (
            <div className="setup-qr">
              <img src={run.qr_code} alt="微信登录二维码" />
              <p>使用微信扫码登录，完成后会自动进入下一步。</p>
            </div>
          )}
          {run?.status === 'running' && (
            <div className="setup-progress" role="status">
              <span>{run.qr_code ? '等待扫码…' : '正在准备下一步…'}</span>
              {!prompt && (
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() => act(() => api.setupCancel(run.id))}
                >
                  退出配置
                </button>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
