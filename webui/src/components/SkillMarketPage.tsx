import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowUpRight,
  CheckCircle2,
  Download,
  LoaderCircle,
  Package,
  RefreshCw,
  Search,
  X,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  marketApi,
  type MarketFeed,
  type MarketSkill,
  type MarketSkillDetail,
  type SkillMarket,
} from '../skill-market-api';
import { SecurityAudits } from './SecurityAudits';
import { skillBody } from './SkillsPage';
import './skill-market.css';

const LABELS = { skills_sh: '英文技能市场', skillhub_cn: '中文技能市场' };
const SOURCES = { skills_sh: 'skills.sh', skillhub_cn: 'SkillHub' };
const LINKS = {
  skills_sh: 'https://www.skills.sh/trending',
  skillhub_cn: 'https://skillhub.cn/skills?sortBy=trending',
};
type InstallState = {
  state: 'installing' | 'installed' | 'error';
  message?: string;
  name?: string;
};

function initialMarket(): SkillMarket {
  try {
    return localStorage.getItem('mclaw.skill-market') === 'skillhub_cn'
      ? 'skillhub_cn'
      : 'skills_sh';
  } catch {
    return 'skills_sh';
  }
}
export function marketCount(value: number, chinese: boolean) {
  if (chinese && value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  if (!chinese && value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
  if (!chinese && value >= 1000) return `${(value / 1000).toFixed(1)}K`;
  return value.toLocaleString();
}
function SkillIcon({ item }: { item: MarketSkill }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [item.icon_url]);
  return (
    <span className="market-skill-icon">
      {item.icon_url && !failed ? (
        <img
          src={item.icon_url}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <Package size={24} aria-label="技能" />
      )}
    </span>
  );
}
function Requirements({ item }: { item: MarketSkill }) {
  return (
    <>
      {item.requires_api_key && <span className="market-requirement">需要 API Key</span>}
      {item.paid && <span className="market-requirement">付费</span>}
    </>
  );
}

export function SkillMarketPage({ onManage }: { onManage: (name: string) => void }) {
  const [market, setMarket] = useState<SkillMarket>(initialMarket);
  const [feed, setFeed] = useState<MarketFeed | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<MarketSkillDetail | null>(null);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [notice, setNotice] = useState('');
  const [actions, setActions] = useState<Record<string, InstallState>>({});
  const sequence = useRef(0);
  const activeInstalls = useRef(new Set<string>());
  const detailPanel = useRef<HTMLElement>(null);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const chinese = market === 'skillhub_cn';
  const limit = feed?.market === market ? feed.limit : 40;

  const load = useCallback(
    async (force = false, quiet = false) => {
      const current = ++sequence.current;
      if (!quiet) {
        setRefreshing(force);
        setError('');
      }
      try {
        const next = await (force
          ? marketApi.refresh(market, timezone)
          : marketApi.trending(market, timezone));
        if (sequence.current !== current) return;
        setFeed(next);
        setError('');
        setSelected((previous) =>
          previous && next.items.some((item) => item.id === previous) ? previous : null,
        );
      } catch (cause) {
        if (sequence.current === current)
          setError(cause instanceof Error ? cause.message : '榜单加载失败，请重试。');
      } finally {
        if (sequence.current === current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [market, timezone],
  );

  useEffect(() => {
    setFeed(null);
    setSelected(null);
    setDetail(null);
    setQuery('');
    setLoading(true);
    void load();
    const tick = () => {
      if (document.visibilityState === 'visible') void load(false, true);
    };
    const timer = window.setInterval(tick, 30000);
    window.addEventListener('focus', tick);
    document.addEventListener('visibilitychange', tick);
    return () => {
      sequence.current++;
      window.clearInterval(timer);
      window.removeEventListener('focus', tick);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [load]);

  const preparingDetails = !!feed?.detail_cache?.pending;
  useEffect(() => {
    if (!preparingDetails) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load(false, true);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [load, preparingDetails]);

  useEffect(() => {
    setDetail(null);
    setDetailError('');
    if (!selected || feed?.market !== market) {
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    setDetailLoading(true);
    const read = async () => {
      try {
        const value = await marketApi.detail(market, selected, controller.signal);
        if (controller.signal.aborted) return;
        setDetail(value);
        setDetailError('');
        if (value.detail_refreshing) timer = window.setTimeout(() => void read(), 1500);
      } catch (cause) {
        if (!controller.signal.aborted)
          setDetailError(cause instanceof Error ? cause.message : '详情加载失败。');
      } finally {
        if (!controller.signal.aborted) setDetailLoading(false);
      }
    };
    void read();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [market, selected, feed?.market]);

  useEffect(() => {
    if (!selected) return;
    const outside = (event: MouseEvent) => {
      if (event.target instanceof Node && !detailPanel.current?.contains(event.target)) {
        // Dismissing the panel must not also install a skill or open another row underneath.
        event.preventDefault();
        event.stopPropagation();
        setSelected(null);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.isComposing) setSelected(null);
    };
    document.addEventListener('click', outside, true);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('click', outside, true);
      document.removeEventListener('keydown', escape);
    };
  }, [selected]);

  const install = async (item: MarketSkill) => {
    if (activeInstalls.current.has(item.url)) return;
    activeInstalls.current.add(item.url);
    setActions((current) => ({ ...current, [item.url]: { state: 'installing' } }));
    try {
      const result = await marketApi.install(item.market, item.id);
      setActions((current) => ({
        ...current,
        [item.url]: { state: 'installed', name: result.name },
      }));
      setNotice(`已安装 ${item.name}${result.audits?.notice ? `。${result.audits.notice}` : ''}`);
      setDetail((current) =>
        current?.url === item.url
          ? {
              ...current,
              installed_name: result.name,
              ...(result.audits ? { audits: result.audits } : {}),
            }
          : current,
      );
    } catch (cause) {
      setActions((current) => ({
        ...current,
        [item.url]: {
          state: 'error',
          message: cause instanceof Error ? cause.message : '安装失败，请重试。',
        },
      }));
    } finally {
      activeInstalls.current.delete(item.url);
    }
  };

  const installButton = (item: MarketSkill, primary = false) => {
    const action = actions[item.url];
    const installed = action?.state === 'installed' || !!item.installed_name;
    const installing = action?.state === 'installing' || !!item.installing;
    return (
      <button
        className={`market-install ${primary ? 'primary' : ''} ${installed ? 'is-installed' : ''}`}
        disabled={installed || installing}
        aria-label={`${installed ? '已安装' : installing ? '正在安装' : action?.state === 'error' ? '重试安装' : '安装'} ${item.name}`}
        onClick={() => void install(item)}
      >
        {installed ? (
          <CheckCircle2 size={17} />
        ) : installing ? (
          <LoaderCircle size={17} className="market-spin" />
        ) : action?.state === 'error' ? (
          <RefreshCw size={17} />
        ) : (
          <Download size={17} />
        )}
        {installed
          ? '已安装'
          : installing
            ? '安装中…'
            : action?.state === 'error'
              ? '重试'
              : primary
                ? '安装到 M-Claw'
                : '安装'}
      </button>
    );
  };

  const filtered = (feed?.items ?? []).filter((item) =>
    `${item.name} ${item.description} ${item.publisher}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  const selectedItem = feed?.items.find((item) => item.id === selected);
  const shown = detail ?? selectedItem;
  const installedName = shown && (actions[shown.url]?.name || shown.installed_name);
  const update =
    feed &&
    new Date(feed.updated_at).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });

  const skillRow = (item: MarketSkill) => (
    <div className={`market-row ${selected === item.id ? 'selected' : ''}`} key={item.id}>
      <span className="market-rank">{item.rank}</span>
      {chinese && <SkillIcon item={item} />}
      <button
        className="market-row-info"
        onClick={() => setSelected(item.id)}
        aria-label={`查看 ${item.name} 详情`}
        aria-pressed={selected === item.id}
      >
        <span className="market-name">
          <span className="market-title-text" title={item.name}>
            {item.name}
          </span>
          {chinese && <Requirements item={item} />}
        </span>
        <span className="market-description" title={chinese ? item.description : item.publisher}>
          {chinese ? item.description : item.publisher}
        </span>
        {actions[item.url]?.state === 'error' && (
          <span className="market-install-error" role="alert">
            {actions[item.url].message}
          </span>
        )}
      </button>
      {chinese && (
        <span className="market-publisher" title={item.publisher}>
          {item.publisher}
        </span>
      )}
      <span className="market-count" title={`${item.count_label}：${item.count.toLocaleString()}`}>
        {marketCount(item.count, chinese)}
      </span>
      <div className="market-row-action">{installButton(item)}</div>
    </div>
  );

  return (
    <section
      className={`management-page skill-market ${chinese ? 'market-chinese' : 'market-english'}`}
      aria-label="技能市场"
    >
      <header className="page-heading market-heading">
        <h2>技能市场</h2>
        <button
          className="market-refresh"
          onClick={() => void load(true)}
          disabled={refreshing || loading}
        >
          <RefreshCw size={16} className={refreshing ? 'market-spin' : ''} />
          {refreshing ? '正在刷新…' : '刷新榜单'}
        </button>
      </header>
      <div className="market-tabs" role="tablist" aria-label="技能市场来源">
        {(['skillhub_cn', 'skills_sh'] as const).map((value) => (
          <button
            key={value}
            role="tab"
            aria-selected={market === value}
            aria-controls="market-panel"
            id={`market-tab-${value}`}
            onClick={() => {
              setMarket(value);
              try {
                localStorage.setItem('mclaw.skill-market', value);
              } catch {
                /* Preference storage is optional. */
              }
            }}
          >
            {LABELS[value]}
          </button>
        ))}
      </div>
      <div
        className="market-workspace"
        id="market-panel"
        role="tabpanel"
        aria-labelledby={`market-tab-${market}`}
      >
        <div className="market-ranking">
          <div className="market-toolbar">
            <div className="market-trending">
              <h2>Trending</h2>
              <span className="market-top">Top {limit}</span>
            </div>
            <p
              className="market-updated"
              title={
                feed
                  ? `下次刷新：${new Date(feed.next_refresh_at).toLocaleString('zh-CN')}`
                  : undefined
              }
            >
              {feed ? `${update} 更新 · ` : ''}每天 08:00 自动刷新（本地时间）
            </p>
            <label className="market-search">
              <Search size={20} />
              <input
                aria-label="搜索榜单技能"
                placeholder={`搜索这 ${feed?.market === market ? feed.items.length : limit} 个技能…`}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
          </div>
          {(error || feed?.warning) && (
            <div className="market-message" role="alert">
              <span>{error || feed?.warning}</span>
              <button onClick={() => void load(true)} disabled={refreshing}>
                重试
              </button>
            </div>
          )}
          <div className="market-list" aria-busy={loading}>
            <div className="market-columns" aria-hidden="true">
              <span>#</span>
              {chinese && <span />}
              <span>技能</span>
              {chinese && <span />}
              <span>{chinese ? '下载量' : '24h 安装量'}</span>
              <span>操作</span>
            </div>
            {loading ? (
              <div className="market-empty" role="status">
                <LoaderCircle size={22} className="market-spin" />
                正在加载榜单…
              </div>
            ) : filtered.length === 0 ? (
              <div className="market-empty">
                {query
                  ? '没有匹配的技能，试试其他关键词。'
                  : error
                    ? '榜单暂时不可用。'
                    : '暂无 Trending 技能。'}
              </div>
            ) : (
              filtered.map(skillRow)
            )}
          </div>
          <footer className="market-footer">
            <span>
              {query ? `找到 ${filtered.length} 个 · ` : ''}共 {feed?.items.length ?? 0} 个技能
              {feed?.detail_cache && feed.detail_cache.pending > 0 && (
                <span role="status">
                  {' '}
                  · {feed.detail_cache.retry_at ? '来源限流，稍后继续准备' : '详情准备中'}{' '}
                  {feed.detail_cache.ready}/{feed.detail_cache.total}
                </span>
              )}
              {feed?.detail_cache &&
                feed.detail_cache.pending === 0 &&
                feed.detail_cache.ready < feed.detail_cache.total && (
                  <span role="status">
                    {' '}
                    · {feed.detail_cache.total - feed.detail_cache.ready} 个详情暂不可用
                  </span>
                )}
            </span>
            <a href={LINKS[market]} target="_blank" rel="noreferrer">
              来源：{SOURCES[market]} <ArrowUpRight size={14} />
            </a>
          </footer>
        </div>
        {selected && shown && (
          <aside className="market-detail" aria-label="技能详情" ref={detailPanel}>
            <header className="market-detail-heading">
              <h2>技能详情</h2>
              <button
                className="icon-button"
                aria-label="关闭技能详情"
                onClick={() => setSelected(null)}
              >
                <X size={21} />
              </button>
            </header>
            <div className="market-detail-title">
              {chinese && <SkillIcon item={shown} />}
              <div>
                <h2>{shown.name}</h2>
                <p>{shown.publisher}</p>
              </div>
            </div>
            <div className="market-detail-meta">
              <span>
                排名 <strong>#{shown.rank}</strong>
              </span>
              <span>
                {shown.count_label} <strong>{marketCount(shown.count, chinese)}</strong>
              </span>
            </div>
            {chinese && (
              <div className="market-detail-requirements">
                <Requirements item={shown} />
              </div>
            )}
            <p className="market-summary">{shown.description}</p>
            <a className="market-source-link" href={shown.url} target="_blank" rel="noreferrer">
              在 {SOURCES[market]} 查看 <ArrowUpRight size={17} />
            </a>
            {detailLoading && (
              <p className="market-detail-loading" role="status">
                <LoaderCircle size={18} className="market-spin" />
                正在读取说明与审核信息…
              </p>
            )}
            {detailError && (
              <p className="market-message" role="alert">
                {detailError}
              </p>
            )}
            {detail && <SecurityAudits audits={detail.audits} />}
            {detail?.detail_updated_at && (
              <p className="market-detail-updated">
                获取于 {new Date(detail.detail_updated_at).toLocaleString('zh-CN')}
                {detail.detail_refreshing
                  ? ' · 正在更新…'
                  : detail.detail_stale
                    ? ' · 展示上次内容'
                    : ''}
              </p>
            )}
            {chinese && (shown.requires_api_key || shown.paid) && (
              <p className="market-usage-note">
                {shown.requires_api_key && '使用该技能需要配置服务方 API Key。'}
                {shown.paid && '使用费用以服务方说明为准。'}
              </p>
            )}
            <div className="market-detail-action">
              {installButton(shown, true)}
              {installedName && (
                <button className="text-button" onClick={() => onManage(installedName)}>
                  在我的技能中查看 <ArrowUpRight size={15} />
                </button>
              )}
            </div>
            {actions[shown.url]?.state === 'error' && (
              <p className="market-install-error" role="alert">
                {actions[shown.url].message}
              </p>
            )}
            {detail?.detail_warning && <p className="market-message">{detail.detail_warning}</p>}
            {detail?.content && (
              <section className="market-instructions">
                <h3>技能说明</h3>
                <div className="markdown">
                  <Markdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: ({ children, href }) => (
                        <a href={href} target="_blank" rel="noreferrer">
                          {children}
                        </a>
                      ),
                    }}
                  >
                    {skillBody(detail.content)}
                  </Markdown>
                </div>
              </section>
            )}
            {detail?.repository_url && (
              <a
                className="market-source-link market-repository"
                href={detail.repository_url}
                target="_blank"
                rel="noreferrer"
              >
                查看源仓库 <ArrowUpRight size={16} />
              </a>
            )}
          </aside>
        )}
      </div>
      {notice && (
        <div className="market-toast" role="status">
          <CheckCircle2 size={18} />
          <span>{notice}</span>
          <button className="icon-button" aria-label="关闭安装提示" onClick={() => setNotice('')}>
            <X size={16} />
          </button>
        </div>
      )}
    </section>
  );
}
