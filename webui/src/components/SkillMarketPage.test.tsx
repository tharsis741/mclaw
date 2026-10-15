import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import {
  marketApi,
  type MarketFeed,
  type MarketSkill,
  type MarketSkillDetail,
  type SkillAudits,
} from '../skill-market-api';
import { SkillMarketPage } from './SkillMarketPage';
import { ReviewValue } from './SkillsPage';

vi.mock('../skill-market-api', () => ({
  marketApi: { trending: vi.fn(), refresh: vi.fn(), detail: vi.fn(), install: vi.fn() },
}));
const reports: SkillAudits = {
  source: 'skills.sh',
  available: true,
  notice: '指令风险检查提示需留意，请查看报告了解详情。',
  checked_at: '2026-10-15T00:00:00Z',
  reports: [
    {
      provider: 'Gen',
      label: '综合安全评估',
      status: 'pass',
      status_text: '通过',
      risk: 'safe',
      alerts: null,
      analyzed_at: '2026-09-15T08:00:00Z',
      report_url: 'https://skills.sh/demo/skills/find-skills/security/agent-trust-hub',
    },
    {
      provider: 'Socket',
      label: '代码安全检查',
      status: 'pass',
      status_text: '0 项告警',
      risk: 'safe',
      alerts: 0,
      analyzed_at: '2026-09-15T08:00:00Z',
      report_url: 'https://skills.sh/demo/skills/find-skills/security/socket',
    },
    {
      provider: 'Snyk',
      label: '指令风险检查',
      status: 'warn',
      status_text: '需留意',
      risk: 'medium',
      alerts: null,
      analyzed_at: '2026-09-15T08:00:00Z',
      report_url: 'https://skills.sh/demo/skills/find-skills/security/snyk',
    },
  ],
};
const chineseReports: SkillAudits = {
  source: 'skillhub.cn',
  available: true,
  notice: '科恩实验室、云鼎实验室均显示安全，无风险。',
  checked_at: '2026-10-15T00:00:00Z',
  reports: [
    {
      provider: '科恩实验室',
      label: '科恩实验室',
      status: 'pass',
      status_text: '安全，无风险',
      risk: 'low',
      alerts: null,
      analyzed_at: '',
      report_url: 'https://tix.qq.com/search/skill?keyword=example-hash',
    },
    {
      provider: '云鼎实验室',
      label: '云鼎实验室',
      status: 'pass',
      status_text: '安全，无风险',
      risk: 'low',
      alerts: null,
      analyzed_at: '',
      report_url:
        'https://static.cloudsec.tencent.com/html-report-v2/example.html?q-sign-time=123%3B456&q-signature=example',
    },
  ],
};
const english: MarketSkill = {
  id: 'demo/skills/find-skills',
  market: 'skills_sh',
  rank: 7,
  name: 'find-skills',
  slug: 'find-skills',
  publisher: 'demo/skills',
  description: 'Discover useful skills.',
  url: 'https://skills.sh/demo/skills/find-skills',
  icon_url: null,
  count: 19000,
  count_label: '24h 安装量',
  version: '',
  requires_api_key: false,
  paid: false,
};
const chinese: MarketSkill = {
  ...english,
  id: 'publisher/office',
  market: 'skillhub_cn',
  rank: 1,
  name: '办公文档助手',
  slug: 'office',
  publisher: 'publisher',
  description: '整理办公文件',
  url: 'https://skillhub.cn/skills/publisher/office',
  icon_url: 'https://cloudcache.tencent-cloud.com/office.png',
  count_label: '下载量',
  version: '1.0.0',
  requires_api_key: true,
  paid: true,
};
const feed = (item: MarketSkill): MarketFeed => ({
  market: item.market,
  limit: 40,
  items: [item],
  updated_at: '2026-10-15T00:00:00Z',
  next_refresh_at: '2026-10-16T08:00:00+08:00',
  timezone: 'Asia/Shanghai',
  stale: false,
  warning: '',
});
const detail = (item = english): MarketSkillDetail => ({
  ...item,
  content: '# Find the right skill\n\nDiscover skills for a specific workflow.',
  repository_url: 'https://github.com/demo/skills',
  audits: item.market === 'skillhub_cn' ? chineseReports : reports,
  detail_warning: '',
});
const englishSkills = (count: number, publisher = 'demo/skills', startRank = 1): MarketSkill[] =>
  Array.from({ length: count }, (_, index) => {
    const rank = startRank + index;
    const slug = `skill-${rank}`;
    return {
      ...english,
      id: `${publisher}/${slug}`,
      url: `https://skills.sh/${publisher}/${slug}`,
      publisher,
      rank,
      name: slug,
      slug,
      count: rank * 1000,
    };
  });

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  vi.mocked(marketApi.trending).mockImplementation(async (market) =>
    feed(market === 'skills_sh' ? english : chinese),
  );
  vi.mocked(marketApi.detail).mockImplementation(async (market) =>
    detail(market === 'skills_sh' ? english : chinese),
  );
  vi.mocked(marketApi.refresh).mockImplementation(async (market) =>
    feed(market === 'skills_sh' ? english : chinese),
  );
  vi.mocked(marketApi.install).mockResolvedValue({
    success: true,
    name: 'find-skills',
    audits: reports,
  });
});

it('opens English details on demand, has text identities, and shows Chinese report labels', async () => {
  const { container } = render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '查看 find-skills 详情' }));
  const panel = await screen.findByRole('complementary', { name: '技能详情' });
  expect(within(panel).getByText('find-skills')).toBeInTheDocument();
  expect(await within(panel).findByText('综合安全评估')).toBeInTheDocument();
  expect(within(panel).getByText('代码安全检查')).toBeInTheDocument();
  expect(within(panel).getByText('指令风险检查')).toBeInTheDocument();
  expect(within(panel).getByText('需留意')).toBeInTheDocument();
  expect(within(panel).getByRole('link', { name: '查看指令风险检查报告' })).toHaveAttribute(
    'href',
    reports.reports[2].report_url,
  );
  expect(container.querySelectorAll('.market-skill-icon')).toHaveLength(0);
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(within(panel).getByRole('button', { name: '安装 find-skills' })).toBeEnabled();
  expect(screen.getByRole('heading', { name: /^Trending$/ })).toBeInTheDocument();
  expect(screen.getByText('Top 40')).toBeInTheDocument();
});

it('switches to the full Chinese ranking with source icons and use requirement badges', async () => {
  const { container } = render(<SkillMarketPage onManage={vi.fn()} />);
  await screen.findByRole('button', { name: '查看 find-skills 详情' });
  await userEvent.click(screen.getByRole('tab', { name: '中文技能市场' }));
  expect(await screen.findByText('办公文档助手')).toBeInTheDocument();
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  expect(screen.getByText('需要 API Key')).toBeInTheDocument();
  expect(screen.getByText('付费')).toBeInTheDocument();
  expect(container.querySelector('img')).toHaveAttribute('src', chinese.icon_url);
  expect(screen.getByText('Trending')).toBeInTheDocument();
  expect(screen.getByText('Top 40')).toBeInTheDocument();
  expect(localStorage.getItem('mclaw.skill-market')).toBe('skillhub_cn');
  fireEvent.error(container.querySelector('img')!);
  expect(container.querySelector('img')).toBeNull();
});

it.each([english, chinese])(
  'opens and closes $market details explicitly and keeps them closed after refresh',
  async (item) => {
    localStorage.setItem('mclaw.skill-market', item.market);
    render(<SkillMarketPage onManage={vi.fn()} />);
    const rowButton = await screen.findByRole('button', { name: `查看 ${item.name} 详情` });
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(marketApi.detail).not.toHaveBeenCalled();

    await userEvent.click(rowButton);
    const panel = await screen.findByRole('complementary', { name: '技能详情' });
    await within(panel).findByText('技能说明');
    expect(marketApi.detail).toHaveBeenCalledExactlyOnceWith(
      item.market,
      item.id,
      expect.any(AbortSignal),
    );
    expect(rowButton).toHaveAttribute('aria-pressed', 'true');
    expect(
      within(rowButton.closest('.market-row') as HTMLElement).getByRole('button', {
        name: `安装 ${item.name}`,
      }),
    ).toBeEnabled();

    await userEvent.click(within(panel).getByRole('button', { name: '关闭技能详情' }));
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(rowButton).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(screen.getByRole('button', { name: '刷新榜单' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新榜单' })).toBeEnabled());
    expect(marketApi.refresh).toHaveBeenCalledExactlyOnceWith(item.market, expect.any(String));
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(marketApi.detail).toHaveBeenCalledTimes(1);
  },
);

it.each([english, chinese])('closes details when switching away from $market', async (item) => {
  localStorage.setItem('mclaw.skill-market', item.market);
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: `查看 ${item.name} 详情` }));
  await screen.findByRole('complementary', { name: '技能详情' });
  const other = item.market === 'skills_sh' ? chinese : english;
  const label = other.market === 'skills_sh' ? '英文技能市场' : '中文技能市场';
  await userEvent.click(screen.getByRole('tab', { name: label }));
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: label }));
  await screen.findByRole('button', { name: `查看 ${other.name} 详情` });
  expect(screen.getByRole('tabpanel', { name: label })).toBeInTheDocument();
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  expect(marketApi.detail).toHaveBeenCalledTimes(1);
});

it('shows both Chinese laboratories and original signed report links in the detail panel', async () => {
  localStorage.setItem('mclaw.skill-market', 'skillhub_cn');
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '查看 办公文档助手 详情' }));
  const panel = await screen.findByRole('complementary', { name: '技能详情' });
  expect(await within(panel).findByText('科恩实验室')).toBeInTheDocument();
  expect(within(panel).getByText('云鼎实验室')).toBeInTheDocument();
  expect(within(panel).getAllByText('安全，无风险')).toHaveLength(2);
  for (const report of chineseReports.reports) {
    expect(within(panel).getByRole('link', { name: `查看${report.label}报告` })).toHaveAttribute(
      'href',
      report.report_url,
    );
  }
  expect(within(panel).getByText('来源：skillhub.cn')).toBeInTheDocument();
  expect(within(panel).getByRole('button', { name: '安装 办公文档助手' })).toBeEnabled();
});

it.each([english, chinese])(
  'lists all forty $market skills in source order and searches the final rank',
  async (item) => {
    localStorage.setItem('mclaw.skill-market', item.market);
    const items = Array.from({ length: 40 }, (_, index) => ({
      ...item,
      id: `${item.id}-${index + 1}`,
      url: `${item.url}-${index + 1}`,
      rank: index + 1,
      name: `${item.name} ${index + 1}`,
    }));
    vi.mocked(marketApi.trending).mockResolvedValue({ ...feed(item), items });
    const { container } = render(<SkillMarketPage onManage={vi.fn()} />);
    await screen.findByRole('button', { name: `查看 ${item.name} 1 详情` });
    expect(container.querySelectorAll('.market-row')).toHaveLength(40);
    expect(screen.getAllByRole('button', { name: /^查看 .* 详情$/ })).toHaveLength(40);
    expect([...container.querySelectorAll('.market-rank')].map((row) => row.textContent)).toEqual(
      Array.from({ length: 40 }, (_, index) => String(index + 1)),
    );
    expect(screen.getByText('共 40 个技能')).toBeInTheDocument();
    const search = screen.getByPlaceholderText('搜索这 40 个技能…');
    await userEvent.type(search, `${item.name} 40`);
    expect(container.querySelectorAll('.market-row')).toHaveLength(1);
    expect(screen.getByRole('button', { name: `查看 ${item.name} 40 详情` })).toBeVisible();
    expect(screen.getByRole('button', { name: `安装 ${item.name} 40` })).toBeEnabled();
    await userEvent.clear(search);
    expect(screen.getAllByRole('button', { name: /^查看 .* 详情$/ })).toHaveLength(40);
  },
);

it.each([english, chinese])(
  'dismisses $market details outside the panel or with Escape and keeps inside clicks open',
  async (item) => {
    localStorage.setItem('mclaw.skill-market', item.market);
    render(<SkillMarketPage onManage={vi.fn()} />);
    const row = await screen.findByRole('button', { name: `查看 ${item.name} 详情` });
    await userEvent.click(row);
    const panel = await screen.findByRole('complementary', { name: '技能详情' });
    await userEvent.click(within(panel).getByRole('heading', { name: '技能详情' }));
    expect(panel).toBeInTheDocument();
    await userEvent.click(screen.getByRole('heading', { name: '技能市场' }));
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(row).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(row);
    await screen.findByRole('complementary', { name: '技能详情' });
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    await userEvent.click(row);
    const outsideInstall = screen
      .getAllByRole('button', { name: `安装 ${item.name}` })
      .find((button) => !button.closest('.market-detail'))!;
    await userEvent.click(outsideInstall);
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(marketApi.install).not.toHaveBeenCalled();
  },
);

it('aborts pending details when dismissed and ignores a late response', async () => {
  let finish!: (value: MarketSkillDetail) => void;
  vi.mocked(marketApi.detail).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '查看 find-skills 详情' }));
  expect(screen.getByRole('status')).toHaveTextContent('正在读取说明与审核信息');
  const signal = vi.mocked(marketApi.detail).mock.calls[0][2];
  fireEvent.click(document.body);
  expect(signal?.aborted).toBe(true);
  await act(async () => finish(detail()));
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  expect(screen.queryByText('技能说明')).not.toBeInTheDocument();
});

it('installs and opens a skill independently of other skills from the same repository', async () => {
  const items = englishSkills(4);
  const child = items[2];
  vi.mocked(marketApi.trending).mockResolvedValue({ ...feed(english), items });
  vi.mocked(marketApi.detail).mockResolvedValue(detail(child));
  vi.mocked(marketApi.install).mockResolvedValue({
    success: true,
    name: child.slug,
    audits: reports,
  });
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '安装 skill-3' }));
  expect(await screen.findByRole('button', { name: '已安装 skill-3' })).toBeDisabled();
  expect(marketApi.install).toHaveBeenCalledExactlyOnceWith('skills_sh', child.id);
  expect(screen.getByRole('button', { name: '安装 skill-1' })).toBeEnabled();
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '查看 skill-3 详情' }));
  const panel = await screen.findByRole('complementary', { name: '技能详情' });
  expect(await within(panel).findByText('技能说明')).toBeInTheDocument();
  expect(marketApi.detail).toHaveBeenCalledExactlyOnceWith(
    'skills_sh',
    child.id,
    expect.any(AbortSignal),
  );
  expect(within(panel).getByText('#3')).toBeInTheDocument();
});

it('includes the Chinese audit notice after a single install click', async () => {
  localStorage.setItem('mclaw.skill-market', 'skillhub_cn');
  vi.mocked(marketApi.install).mockResolvedValue({
    success: true,
    name: 'office',
    audits: { ...chineseReports, notice: '云鼎实验室提示需留意，请查看报告了解详情。' },
  });
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '安装 办公文档助手' }));
  expect(await screen.findByRole('status')).toHaveTextContent(
    '已安装 办公文档助手。云鼎实验室提示需留意，请查看报告了解详情。',
  );
  expect(marketApi.install).toHaveBeenCalledExactlyOnceWith('skillhub_cn', chinese.id);
  expect(screen.getByRole('button', { name: '已安装 办公文档助手' })).toBeDisabled();
});

it('filters only the loaded ranking and preserves source rank when manually refreshed', async () => {
  localStorage.setItem('mclaw.skill-market', 'skillhub_cn');
  render(<SkillMarketPage onManage={vi.fn()} />);
  await screen.findByText('办公文档助手');
  await userEvent.type(screen.getByRole('textbox', { name: '搜索榜单技能' }), 'missing');
  expect(screen.getByText('没有匹配的技能，试试其他关键词。')).toBeInTheDocument();
  expect(marketApi.trending).toHaveBeenCalledTimes(1);
  await userEvent.clear(screen.getByRole('textbox', { name: '搜索榜单技能' }));
  vi.mocked(marketApi.refresh).mockResolvedValue(feed({ ...chinese, rank: 12 }));
  await userEvent.click(screen.getByRole('button', { name: '刷新榜单' }));
  expect(await screen.findByText('12')).toBeInTheDocument();
  expect(marketApi.refresh).toHaveBeenCalledWith('skillhub_cn', expect.any(String));
});

it.each([english, chinese])(
  'installs $market skills without opening details or allowing duplicate clicks',
  async (item) => {
    localStorage.setItem('mclaw.skill-market', item.market);
    let finish!: (value: { success: boolean; name: string; audits: SkillAudits }) => void;
    vi.mocked(marketApi.install).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const onManage = vi.fn();
    render(<SkillMarketPage onManage={onManage} />);
    const button = await screen.findByRole('button', { name: `安装 ${item.name}` });
    await userEvent.click(button);
    expect(screen.getByRole('button', { name: `正在安装 ${item.name}` })).toBeDisabled();
    await userEvent.click(button);
    expect(marketApi.install).toHaveBeenCalledTimes(1);
    expect(marketApi.install).toHaveBeenCalledWith(item.market, item.id);
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    expect(marketApi.detail).not.toHaveBeenCalled();
    await act(async () =>
      finish({
        success: true,
        name: item.slug,
        audits: item.market === 'skills_sh' ? reports : chineseReports,
      }),
    );
    expect(screen.getByRole('button', { name: `已安装 ${item.name}` })).toBeDisabled();
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: `查看 ${item.name} 详情` }));
    await userEvent.click(await screen.findByRole('button', { name: '在我的技能中查看' }));
    expect(onManage).toHaveBeenCalledWith(item.slug);
  },
);

it('keeps failed installs retryable and shows the server message', async () => {
  vi.mocked(marketApi.install).mockRejectedValueOnce(new Error('网络连接失败'));
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(await screen.findByRole('button', { name: '安装 find-skills' }));
  expect((await screen.findAllByText('网络连接失败')).length).toBeGreaterThan(0);
  await userEvent.click(screen.getByRole('button', { name: '重试安装 find-skills' }));
  await waitFor(() => expect(marketApi.install).toHaveBeenCalledTimes(2));
  expect(
    (await screen.findAllByRole('button', { name: '已安装 find-skills' })).length,
  ).toBeGreaterThan(0);
});

it('displays the last successful ranking and a refresh failure notice', async () => {
  vi.mocked(marketApi.trending).mockResolvedValue({
    ...feed(english),
    stale: true,
    warning: '正在展示上次成功更新的榜单。',
  });
  render(<SkillMarketPage onManage={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('上次成功更新');
  expect(screen.getByRole('button', { name: '查看 find-skills 详情' })).toBeInTheDocument();
});

it.each([false, true])(
  'keeps the ranking usable while details prepare or cool down (%s)',
  async (limited) => {
    vi.useFakeTimers();
    const prepared = { ...feed(english), detail_cache: { ready: 1, total: 1, pending: 0 } };
    vi.mocked(marketApi.trending)
      .mockResolvedValueOnce({
        ...feed(english),
        detail_cache: {
          ready: 0,
          total: 1,
          pending: 1,
          ...(limited ? { retry_at: '2026-10-15T03:00:00Z' } : {}),
        },
      })
      .mockResolvedValue(prepared);
    const view = render(<SkillMarketPage onManage={vi.fn()} />);
    try {
      await act(async () => {});
      expect(screen.getByRole('status')).toHaveTextContent(
        limited ? '来源限流，稍后继续准备 0/1' : '详情准备中 0/1',
      );
      expect(screen.getByRole('button', { name: '安装 find-skills' })).toBeEnabled();
      fireEvent.click(screen.getByRole('button', { name: '查看 find-skills 详情' }));
      await act(async () => {});
      expect(screen.getByRole('complementary', { name: '技能详情' })).toBeInTheDocument();
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(screen.queryByText(/详情准备中/)).not.toBeInTheDocument();
      expect(screen.getByRole('complementary', { name: '技能详情' })).toBeInTheDocument();
      expect(marketApi.trending).toHaveBeenCalledTimes(2);
      await act(async () => vi.advanceTimersByTimeAsync(4000));
      expect(marketApi.trending).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  },
);

it.each([false, true])(
  'shows cached details immediately and handles background updates when dismissed=%s',
  async (dismiss) => {
    vi.useFakeTimers();
    vi.mocked(marketApi.detail)
      .mockResolvedValueOnce({
        ...detail(),
        content: '# 已缓存的说明',
        detail_refreshing: true,
        detail_stale: true,
        detail_updated_at: '2026-10-15T00:00:00Z',
      })
      .mockResolvedValue({
        ...detail(),
        content: '# 最新的说明',
        detail_refreshing: false,
        detail_stale: false,
        detail_updated_at: '2026-10-15T00:15:00Z',
      });
    const view = render(<SkillMarketPage onManage={vi.fn()} />);
    try {
      await act(async () => {});
      fireEvent.click(screen.getByRole('button', { name: '查看 find-skills 详情' }));
      await act(async () => {});
      expect(screen.getByRole('heading', { name: '已缓存的说明' })).toBeInTheDocument();
      expect(screen.getByText(/正在更新…/)).toBeInTheDocument();
      expect(screen.queryByText('正在读取说明与审核信息…')).not.toBeInTheDocument();
      if (dismiss) fireEvent.click(screen.getByRole('heading', { name: '技能市场' }));
      await act(async () => vi.advanceTimersByTimeAsync(1500));
      if (dismiss) {
        expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
        expect(marketApi.detail).toHaveBeenCalledTimes(1);
      } else {
        expect(screen.getByRole('heading', { name: '最新的说明' })).toBeInTheDocument();
        expect(screen.queryByText(/正在更新…/)).not.toBeInTheDocument();
        expect(marketApi.detail).toHaveBeenCalledTimes(2);
      }
      await act(async () => vi.advanceTimersByTimeAsync(5000));
      expect(marketApi.detail).toHaveBeenCalledTimes(dismiss ? 1 : 2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  },
);

it('distinguishes unavailable source details from a completed cache', async () => {
  vi.mocked(marketApi.trending).mockResolvedValue({
    ...feed(english),
    detail_cache: { ready: 28, total: 40, pending: 0 },
  });
  render(<SkillMarketPage onManage={vi.fn()} />);
  await screen.findByRole('button', { name: '查看 find-skills 详情' });
  expect(screen.getByRole('status')).toHaveTextContent('12 个详情暂不可用');
  expect(screen.getByRole('button', { name: '查看 find-skills 详情' })).toBeEnabled();
});

it('does not let a late English feed replace the Chinese market after switching', async () => {
  let finish!: (value: MarketFeed) => void;
  vi.mocked(marketApi.trending).mockImplementation((market) =>
    market === 'skills_sh'
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : Promise.resolve(feed(chinese)),
  );
  render(<SkillMarketPage onManage={vi.fn()} />);
  await userEvent.click(screen.getByRole('tab', { name: '中文技能市场' }));
  await screen.findByText('办公文档助手');
  await act(async () => finish(feed(english)));
  expect(screen.queryByRole('button', { name: '查看 find-skills 详情' })).not.toBeInTheDocument();
  expect(screen.getByText('办公文档助手')).toBeInTheDocument();
});

it('shares audit presentation with chat install review and keeps missing reports neutral', () => {
  render(
    <ReviewValue
      value={{
        audits: {
          ...reports,
          available: false,
          reports: reports.reports.map((row) => ({
            ...row,
            status: 'unknown',
            status_text: '暂无报告',
          })),
          notice: '暂未获取到审核报告，可在来源网站查看。',
        },
      }}
    />,
  );
  expect(screen.getAllByText('暂无报告')).toHaveLength(3);
  expect(screen.queryByText('通过')).not.toBeInTheDocument();
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
});

it('keeps a pending Chinese report neutral and does not render missing report URLs', () => {
  const { container } = render(
    <ReviewValue
      value={{
        audits: {
          ...chineseReports,
          reports: [
            { ...chineseReports.reports[0], report_url: '' },
            { ...chineseReports.reports[1], status: 'unknown', status_text: '扫描中' },
          ],
          notice: '第三方审核结果供安装时参考。',
        },
      }}
    />,
  );
  expect(screen.getByText('扫描中').closest('.audit-row')).toHaveClass('audit-unknown');
  expect(container.querySelectorAll('.audit-pass')).toHaveLength(1);
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
});
