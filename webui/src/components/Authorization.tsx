import { useState } from 'react';
import { FolderLock, KeyRound } from 'lucide-react';
import type { PendingRequest } from '../types';
import { SkillInstallRequest } from './SkillInstallRequest';

export function Authorization({
  request,
  onReply,
  busy,
}: {
  request: PendingRequest;
  onReply: (value: unknown) => Promise<void>;
  busy: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [authorized, setAuthorized] = useState<string[]>([]);
  const needs = (request.needs ?? []).map((need) => ({
    ...need,
    has_value: need.state ? need.state === 'authorize' : !!need.has_value,
  }));
  const reply = async (value: unknown) => {
    try {
      await onReply(value);
      setValues({});
    } catch {
      /* Keep the dedicated form while the parent displays the request error. */
    }
  };
  if (request.kind === 'skill_install')
    return <SkillInstallRequest request={request} onReply={onReply} busy={busy} />;
  return (
    <section className="authorization-card" aria-label={request.title}>
      <div className="authorization-title">
        {request.kind === 'workspace' ? <FolderLock size={19} /> : <KeyRound size={19} />}
        <h2>{request.title}</h2>
        <span>等待确认</span>
      </div>
      {request.description && <p>{request.description}</p>}
      {request.kind === 'workspace' ? (
        <>
          <code className="workspace-path">{request.workspace}</code>
          <div className="authorization-actions">
            <button className="text-button" disabled={busy} onClick={() => void reply(false)}>
              拒绝
            </button>
            <button className="primary" disabled={busy} onClick={() => void reply(true)}>
              信任并继续
            </button>
          </div>
        </>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const active = needs.filter((need) => authorized.includes(need.env_var));
            void reply({
              values: Object.fromEntries(
                active
                  .filter((need) => values[need.env_var])
                  .map((need) => [need.env_var, values[need.env_var]]),
              ),
              authorized,
              skipped: needs
                .filter((need) => !authorized.includes(need.env_var))
                .map((need) => need.env_var),
            });
          }}
        >
          <p className="authorization-scope">用途与作用域：{request.required_for}</p>
          {needs.map((need) => (
            <div className="secret-need" key={need.env_var}>
              <label className="secret-checkbox">
                <input
                  type="checkbox"
                  checked={authorized.includes(need.env_var)}
                  onChange={(event) =>
                    setAuthorized((current) =>
                      event.target.checked
                        ? [...current, need.env_var]
                        : current.filter((key) => key !== need.env_var),
                    )
                  }
                />
                <span>
                  授权 {need.provider ?? need.env_var}
                  {need.has_value && <small>已保存凭据</small>}
                </span>
              </label>
              {need.purpose && <p>{need.purpose}</p>}
              {!need.has_value && (
                <label className="field-label">
                  {need.env_var}
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={values[need.env_var] ?? ''}
                    onChange={(event) => {
                      setValues((current) => ({ ...current, [need.env_var]: event.target.value }));
                      if (event.target.value)
                        setAuthorized((current) =>
                          current.includes(need.env_var) ? current : [...current, need.env_var],
                        );
                    }}
                  />
                </label>
              )}
            </div>
          ))}
          <div className="authorization-actions">
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() =>
                void reply({
                  values: {},
                  authorized: [],
                  skipped: needs.map((need) => need.env_var),
                })
              }
            >
              跳过
            </button>
            <button
              className="primary"
              disabled={
                busy ||
                !authorized.length ||
                needs.some(
                  (need) =>
                    authorized.includes(need.env_var) && !need.has_value && !values[need.env_var],
                )
              }
            >
              授权并继续
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
