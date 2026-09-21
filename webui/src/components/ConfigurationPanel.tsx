import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, LoaderCircle, RefreshCw } from 'lucide-react';
import { ApiError, settingsApi } from '../api';
import type {
  ConfigurationField,
  ConfigurationPage,
  ConfigurationSnapshot,
  ConfigurationValue,
} from '../types';
import { Select } from './Select';
import './ConfigurationPanel.css';

type Drafts = Record<string, ConfigurationValue>;
const display = (value: ConfigurationValue) => value ?? '';

export function ConfigurationPanel({
  page,
  onSaved,
}: {
  page: ConfigurationPage | null;
  onSaved?: (snapshot: ConfigurationSnapshot) => void;
}) {
  const [snapshot, setSnapshot] = useState<ConfigurationSnapshot | null>(null);
  const [drafts, setDrafts] = useState<Drafts>({});
  const draftRef = useRef(drafts);
  draftRef.current = drafts;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
  const generation = useRef(0);

  const load = useCallback(async (keepDrafts = true) => {
    const request = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const result = await settingsApi.configuration();
      if (request !== generation.current) return;
      setSnapshot(result);
      setConflict(false);
      if (!keepDrafts) setDrafts({});
      else {
        setDrafts((current) => {
          const values = new Map(
            result.groups.flatMap((group) =>
              group.fields.map((field) => [field.path, display(field.value)] as const),
            ),
          );
          return Object.fromEntries(
            Object.entries(current).filter(([path, value]) => values.get(path) !== value),
          );
        });
      }
    } catch (e) {
      if (request === generation.current) setError(e instanceof Error ? e.message : '读取设置失败');
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    setNotice('');
    if (page && !Object.keys(draftRef.current).length) void load();
  }, [page, load]);
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  const groups = snapshot?.groups.filter((group) => group.page === page) ?? [];
  const paths = new Set(groups.flatMap((group) => group.fields.map((field) => field.path)));
  const changes = Object.fromEntries(Object.entries(drafts).filter(([path]) => paths.has(path)));
  const dirty = Object.keys(changes).length > 0;
  const change = (field: ConfigurationField, value: ConfigurationValue) => {
    setNotice('');
    setDrafts((current) => {
      const result = { ...current };
      if (value === display(field.value)) delete result[field.path];
      else result[field.path] = value;
      return result;
    });
  };
  const save = async () => {
    if (!snapshot || !dirty) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await settingsApi.saveConfiguration(snapshot.revision, changes);
      setSnapshot(result);
      setDrafts((current) =>
        Object.fromEntries(Object.entries(current).filter(([path]) => !paths.has(path))),
      );
      setConflict(false);
      setNotice('已保存');
      onSaved?.(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败');
      setConflict(e instanceof ApiError && e.code === 'CONFIGURATION_CONFLICT');
    } finally {
      setBusy(false);
    }
  };

  const row = (field: ConfigurationField) => {
    const value = drafts[field.path] ?? display(field.value);
    const labelId = `configuration-${field.path}`;
    return (
      <div className="setting-row configuration-row" key={field.path}>
        <div className="configuration-label">
          <label id={labelId}>{field.label}</label>
          {field.description && <small>{field.description}</small>}
          {field.credential_url && <small>地址已配置；填写新地址后替换。</small>}
        </div>
        {field.kind === 'boolean' ? (
          <input
            type="checkbox"
            role="switch"
            className="tool-switch"
            aria-labelledby={labelId}
            checked={value === true}
            disabled={busy}
            onChange={(event) => change(field, event.target.checked)}
          />
        ) : field.kind === 'select' || field.kind === 'timezone' || field.kind === 'provider' ? (
          <Select
            label={field.label}
            value={String(value)}
            disabled={busy}
            searchable={field.kind === 'timezone' || field.kind === 'provider'}
            editable={field.kind === 'timezone'}
            onChange={(selected) => change(field, selected)}
          >
            {(field.kind === 'timezone'
              ? [
                  'Asia/Shanghai',
                  'Asia/Hong_Kong',
                  'Asia/Tokyo',
                  'UTC',
                  'Europe/London',
                  'America/New_York',
                ].map((zone) => ({ value: zone, label: zone }))
              : field.options
            ).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
            {field.kind === 'select' && !field.options.some((option) => option.value === value) && (
              <option value={String(value)}>{String(value)}（当前配置）</option>
            )}
          </Select>
        ) : (
          <input
            aria-labelledby={labelId}
            type={field.kind === 'number' ? 'number' : field.kind === 'url' ? 'url' : 'text'}
            className={field.kind === 'number' ? 'configuration-number' : 'configuration-text'}
            value={String(value)}
            disabled={busy}
            autoComplete="off"
            required={field.kind === 'number'}
            min={field.minimum ?? undefined}
            max={field.maximum ?? undefined}
            step={field.step ?? undefined}
            onChange={(event) =>
              change(
                field,
                field.kind === 'number' && event.target.value !== ''
                  ? Number(event.target.value)
                  : event.target.value,
              )
            }
          />
        )}
      </div>
    );
  };

  return (
    <form
      className="configuration-panel"
      noValidate
      hidden={!page}
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      {!snapshot && busy && <p className="list-empty">正在读取设置…</p>}
      {groups.map((group) => (
        <section className="settings-section" key={group.id}>
          <div className="configuration-heading">
            <h3>{group.title}</h3>
            <span>{group.effect}</span>
          </div>
          <div className="setting-rows">
            {group.fields.filter((field) => !field.advanced).map(row)}
          </div>
          {group.fields.some((field) => field.advanced) && (
            <details className="configuration-advanced">
              <summary>高级设置</summary>
              <div className="setting-rows">
                {group.fields.filter((field) => field.advanced).map(row)}
              </div>
            </details>
          )}
        </section>
      ))}
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {conflict && (
        <p className="configuration-conflict">本次修改已保留。读取最新配置后，请核对再保存。</p>
      )}
      <div className="configuration-actions">
        <span role="status">
          {notice ? (
            <>
              <Check size={15} />
              {notice}
            </>
          ) : dirty ? (
            '有未保存的修改'
          ) : (
            ''
          )}
        </span>
        <button
          type="button"
          className="text-button"
          disabled={busy}
          onClick={() => {
            void load(true);
            setNotice('');
          }}
        >
          <RefreshCw size={14} />
          读取最新配置
        </button>
        <button type="submit" className="primary" disabled={busy || !dirty || conflict}>
          {busy && <LoaderCircle size={14} className="spin" />}保存设置
        </button>
      </div>
    </form>
  );
}
