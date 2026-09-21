import { Select } from './Select';
import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, Mic, Square, X } from 'lucide-react';
import { voiceApi } from '../api';
import type { VoiceStatus } from '../types';

export function VoiceInput({
  draftKey,
  disabled,
  onTranscript,
}: {
  draftKey: string;
  disabled: boolean;
  onTranscript: (key: string, text: string) => void;
}) {
  const [phase, setPhase] = useState<'idle' | 'preparing' | 'recording' | 'transcribing'>('idle');
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState('');
  const [configuration, setConfiguration] = useState<VoiceStatus | null>(null);
  const [provider, setProvider] = useState('qwen');
  const [key, setKey] = useState('');
  const [saving, setSaving] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const mounted = useRef(true);
  const callback = useRef(onTranscript);
  callback.current = onTranscript;
  const cleanup = () => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  };
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (recorder.current?.state === 'recording') {
        recorder.current.onstop = null;
        recorder.current.stop();
      }
      cleanup();
    };
  }, []);
  const start = async () => {
    const target = draftKey;
    setError('');
    setPhase('preparing');
    try {
      const status = await voiceApi.status();
      if (!mounted.current) return;
      if (!status.ready || !status.enabled) {
        setConfiguration(status);
        setProvider(status.provider);
        setPhase('idle');
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined')
        throw new Error('当前浏览器不支持录音，请使用支持麦克风的浏览器。');
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mounted.current) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = media;
      const mimeType = status.mime_types.find((mime) => MediaRecorder.isTypeSupported(mime));
      if (!mimeType) throw new Error('当前浏览器的录音格式暂不可用，请换一个浏览器重试。');
      const next = new MediaRecorder(media, { mimeType });
      recorder.current = next;
      const chunks: Blob[] = [];
      const started = Date.now();
      let bytes = 0;
      let overLimit = false;
      next.ondataavailable = (event) => {
        if (!event.data.size) return;
        chunks.push(event.data);
        bytes += event.data.size;
        if (bytes > status.max_bytes && next.state === 'recording') {
          overLimit = true;
          next.stop();
        }
      };
      next.onerror = () => {
        cleanup();
        setError('录音中断，请检查麦克风后重试。');
        setPhase('idle');
      };
      next.onstop = async () => {
        const duration = Math.min(Date.now() - started, status.max_seconds * 1000);
        cleanup();
        if (!mounted.current) return;
        setPhase('transcribing');
        try {
          if (overLimit || bytes > status.max_bytes)
            throw new Error('录音文件过大，请缩短录音后重试。');
          const audio = new Blob(chunks, { type: mimeType });
          if (!audio.size) throw new Error('没有录到声音，请检查麦克风后重试。');
          const result = await voiceApi.transcribe(audio, duration);
          // The draft that started the recording owns the result, even after navigating elsewhere.
          callback.current(target, result.text);
          if (mounted.current && !result.text.trim()) setError('没有识别出文字，请重试。');
        } catch (e) {
          if (mounted.current) setError(e instanceof Error ? e.message : '转写失败，请重试。');
        } finally {
          if (mounted.current) setPhase('idle');
        }
      };
      next.start(1000);
      setSeconds(0);
      setPhase('recording');
      timer.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - started) / 1000);
        setSeconds(elapsed);
        if (elapsed >= status.max_seconds && next.state === 'recording') next.stop();
      }, 250);
    } catch (e) {
      cleanup();
      if (!mounted.current) return;
      setError(
        e instanceof DOMException && e.name === 'NotAllowedError'
          ? '麦克风权限未开启，可在浏览器设置中允许后重试。'
          : e instanceof Error
            ? e.message
            : '无法开始录音',
      );
      setPhase('idle');
    }
  };
  return (
    <div className="voice-input">
      <button
        type="button"
        className={`icon-button ${phase === 'recording' ? 'recording' : ''}`}
        title={phase === 'recording' ? '结束录音并转写' : '语音输入'}
        aria-label={phase === 'recording' ? '结束录音并转写' : '语音输入'}
        disabled={
          phase === 'preparing' || phase === 'transcribing' || (disabled && phase !== 'recording')
        }
        onClick={() => (phase === 'recording' ? recorder.current?.stop() : void start())}
      >
        {phase === 'recording' ? (
          <Square size={16} fill="currentColor" />
        ) : phase === 'idle' ? (
          <Mic size={18} />
        ) : (
          <LoaderCircle className="spin" size={18} />
        )}
      </button>
      {phase !== 'idle' && (
        <span className="voice-progress" role="status">
          {phase === 'recording'
            ? `录音 ${seconds} 秒 · 点击结束`
            : phase === 'transcribing'
              ? '正在转成文字…'
              : '正在准备麦克风…'}
        </span>
      )}
      {error && (
        <div className="voice-error" role="alert">
          {error}
          <button
            type="button"
            className="icon-button"
            aria-label="关闭录音提示"
            onClick={() => setError('')}
          >
            <X size={14} />
          </button>
        </div>
      )}
      {configuration && (
        <div className="modal-backdrop">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="voice-configuration-title"
          >
            <button
              type="button"
              className="icon-button modal-close"
              aria-label="关闭语音设置"
              onClick={() => {
                setConfiguration(null);
                setKey('');
              }}
            >
              <X size={18} />
            </button>
            <h2 id="voice-configuration-title">开启语音输入</h2>
            <p>{configuration.notice}</p>
            <p className="muted">
              每次最多录音 {configuration.max_seconds} 秒。转写后的文字会加入草稿，编辑后再发送。
            </p>
            <label className="field-label">
              服务区域
              <Select
                label="服务区域"
                value={provider}
                onChange={(selectedValue) => setProvider(selectedValue)}
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
                value={key}
                onChange={(event) => setKey(event.target.value)}
                placeholder={
                  configuration.credential_configured
                    ? '已保存，留空复用'
                    : '填写对应区域的 API Key'
                }
              />
            </label>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            <button
              type="button"
              className="primary"
              disabled={saving}
              onClick={async () => {
                setSaving(true);
                setError('');
                try {
                  await voiceApi.configure({
                    enabled: true,
                    provider,
                    ...(key.trim() ? { api_key: key.trim() } : {}),
                  });
                  setKey('');
                  setConfiguration(null);
                } catch (e) {
                  setError(e instanceof Error ? e.message : '保存失败');
                } finally {
                  setSaving(false);
                }
              }}
            >
              {saving ? '正在保存…' : '保存并开启'}
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
