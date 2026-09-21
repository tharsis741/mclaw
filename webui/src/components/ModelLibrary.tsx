import { Select } from './Select';
import { useEffect, useState } from 'react';
import { Search } from 'lucide-react';
import { providersApi } from '../api';
import type { ModelLibrarySnapshot } from '../types';

export function ModelLibrary({ updatedAt }: { updatedAt?: number | null }) {
  const [query, setQuery] = useState('');
  const [provider, setProvider] = useState('');
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ModelLibrarySnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const request = new AbortController();
    setLoading(true);
    setError('');
    const timer = setTimeout(
      () => {
        void providersApi
          .library(query, provider, offset, request.signal)
          .then((next) => {
            if (!request.signal.aborted) {
              setData(next);
              setOffset(next.offset);
            }
          })
          .catch((e) => {
            if (!request.signal.aborted)
              setError(e instanceof Error ? e.message : '读取模型库失败');
          })
          .finally(() => {
            if (!request.signal.aborted) setLoading(false);
          });
      },
      query ? 200 : 0,
    );
    return () => {
      clearTimeout(timer);
      request.abort();
    };
  }, [query, provider, offset, updatedAt, retry]);
  const selected = data?.providers.find((item) => item.id === provider);
  return (
    <div className="model-library">
      <div className="model-library-filters">
        <label className="provider-search">
          <Search size={17} aria-hidden="true" />
          <input
            aria-label="搜索模型库"
            placeholder="搜索供应商或模型"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setOffset(0);
            }}
          />
        </label>
        <Select
          label="筛选模型库供应商"
          searchable
          value={provider}
          onChange={(selectedValue) => {
            setProvider(selectedValue);
            setOffset(0);
          }}
        >
          <option value="">全部供应商</option>
          {data?.providers.map((item) => (
            <option value={item.id} key={item.id}>
              {item.label}
            </option>
          ))}
        </Select>
      </div>
      {selected && (
        <div className="model-library-profiles">
          <span>{selected.id}</span>
          {selected.profiles.map((item) => (
            <span key={`${item.provider}:${item.profile}`}>
              {item.region}
              {item.callable ? '' : ' · 仅模型库'}
            </span>
          ))}
        </div>
      )}
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button className="text-button" onClick={() => setRetry((value) => value + 1)}>
            重试
          </button>
        </div>
      )}
      {loading && (
        <p className="provider-loading" role="status">
          正在读取模型库…
        </p>
      )}
      {!loading && !error && data && (
        <>
          <p className="model-library-count">
            {data.total.toLocaleString()} 个模型 · 来源 models.dev
          </p>
          <div className="model-library-results">
            {data.models.map((model) => (
              <article key={`${model.provider}:${model.id}`}>
                <div>
                  <strong>{model.name}</strong>
                  <small>{model.provider_label}</small>
                </div>
                <code>{model.id}</code>
                {model.context && <small>上下文 {model.context.toLocaleString()} tokens</small>}
              </article>
            ))}
            {!data.total && (
              <p className="provider-empty">
                {data.status.models
                  ? '没有找到匹配的供应商或模型。'
                  : '还没有模型库，点击上方“刷新模型库”获取。'}
              </p>
            )}
          </div>
          {data.total > data.limit && (
            <nav className="model-library-pagination" aria-label="模型库分页">
              <button
                className="secondary"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - data.limit))}
              >
                上一页
              </button>
              <span>
                {Math.floor(offset / data.limit) + 1} / {Math.ceil(data.total / data.limit)}
              </span>
              <button
                className="secondary"
                disabled={offset + data.limit >= data.total}
                onClick={() => setOffset(offset + data.limit)}
              >
                下一页
              </button>
            </nav>
          )}
        </>
      )}
    </div>
  );
}
