import { BrandLogo } from './BrandLogo';
import { BrandWordmark } from './BrandWordmark';

export function StartupScreen({
  leaving = false,
  ready = false,
  error = '',
  onRetry,
}: {
  leaving?: boolean;
  ready?: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <section className={`startup-screen${leaving ? ' is-leaving' : ''}`} aria-label="打开 M-Claw">
      <div className="startup-brand">
        <BrandLogo />
        <h1>
          <BrandWordmark />
        </h1>
      </div>
      <div className={`startup-status${error ? ' has-error' : ''}`}>
        {!error && <span className="startup-track" aria-hidden="true" />}
        <p role={error ? 'alert' : 'status'}>
          {error || (ready ? '即将打开' : '正在连接本机服务…')}
        </p>
        {error && (
          <button className="startup-retry" onClick={onRetry}>
            重新连接
          </button>
        )}
      </div>
    </section>
  );
}
