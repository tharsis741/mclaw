import { useEffect, useState } from 'react';
import { LoaderCircle, Power, X } from 'lucide-react';
import { api, ApiError } from '../api';

export function ServiceControls({ onRequested }: { onRequested: () => void }) {
  const [phase, setPhase] = useState<
    'idle' | 'confirm' | 'requesting' | 'stopping' | 'disconnected'
  >('idle');
  const [error, setError] = useState('');
  useEffect(() => {
    if (phase !== 'stopping') return;
    let fetching = false;
    const timer = setInterval(() => {
      if (fetching) return;
      fetching = true;
      void api
        .health()
        .catch((error) => {
          if (error instanceof ApiError && error.status === 0) setPhase('disconnected');
        })
        .finally(() => {
          fetching = false;
        });
    }, 1000);
    return () => clearInterval(timer);
  }, [phase]);
  const stop = async () => {
    setPhase('requesting');
    setError('');
    try {
      await api.stopService();
      onRequested();
      setPhase('stopping');
    } catch (error) {
      setError(error instanceof Error ? error.message : '退出请求失败');
      setPhase('confirm');
    }
  };
  return (
    <>
      <div className="service-controls setting-row">
        <div>
          <Power size={20} />
          <span>
            <strong>服务运行</strong>
            <small className="service-runtime-status">
              {phase === 'disconnected'
                ? '已断开'
                : phase === 'requesting' || phase === 'stopping'
                  ? '正在退出…'
                  : '运行中'}
            </small>
          </span>
        </div>
        <button
          className="text-button"
          disabled={phase !== 'idle'}
          onClick={() => setPhase('confirm')}
        >
          退出 M-Claw
        </button>
      </div>
      {phase !== 'idle' && (
        <div className="modal-backdrop">
          <section
            className="modal service-stop-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="service-stop-title"
          >
            {phase === 'confirm' && (
              <button
                className="icon-button modal-close"
                aria-label="取消退出服务"
                onClick={() => setPhase('idle')}
              >
                <X size={18} />
              </button>
            )}
            {phase === 'stopping' || phase === 'requesting' ? (
              <LoaderCircle size={26} className="spin" />
            ) : (
              <Power size={26} />
            )}
            <h2 id="service-stop-title">
              {phase === 'disconnected'
                ? 'M-Claw 连接已断开'
                : phase === 'stopping' || phase === 'requesting'
                  ? '正在退出 M-Claw'
                  : '退出 M-Claw？'}
            </h2>
            <p className="muted">
              {phase === 'disconnected'
                ? '再次使用时，在终端运行 mclaw 即可。'
                : phase === 'stopping' || phase === 'requesting'
                  ? '正在保存会话并等待任务停止。'
                  : '退出后，当前任务会停止，微信、钉钉等消息渠道会断开。会话记录保留。再次使用时，在终端运行 mclaw 即可。'}
            </p>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            {phase === 'confirm' && (
              <div className="service-stop-actions">
                <button className="text-button" onClick={() => setPhase('idle')}>
                  继续使用
                </button>
                <button className="primary" onClick={() => void stop()}>
                  退出 M-Claw
                </button>
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}
