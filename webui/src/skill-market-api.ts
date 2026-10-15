import { request } from './api';

export type SkillMarket = 'skills_sh' | 'skillhub_cn';
export interface SkillAuditReport {
  provider: string;
  label: string;
  status: 'pass' | 'warn' | 'unknown';
  status_text: string;
  risk: string;
  alerts: number | null;
  analyzed_at: string;
  report_url: string;
}
export interface SkillAudits {
  source: string;
  available: boolean;
  reports: SkillAuditReport[];
  notice: string;
  checked_at: string;
}
export interface MarketSkill {
  id: string;
  market: SkillMarket;
  rank: number;
  slug: string;
  name: string;
  description: string;
  publisher: string;
  url: string;
  icon_url: string | null;
  count: number;
  count_label: string;
  version: string;
  requires_api_key: boolean;
  paid: boolean;
  installed_name?: string | null;
  installing?: boolean;
}
export interface MarketFeed {
  market: SkillMarket;
  limit: number;
  items: MarketSkill[];
  updated_at: string;
  next_refresh_at: string;
  timezone: string;
  stale: boolean;
  warning: string;
  detail_cache?: { ready: number; total: number; pending: number; retry_at?: string };
}
export interface MarketSkillDetail extends MarketSkill {
  content: string;
  repository_url: string | null;
  audits: SkillAudits;
  detail_warning: string;
  detail_updated_at?: string;
  detail_refreshing?: boolean;
  detail_stale?: boolean;
}
export interface MarketInstallResult {
  success: boolean;
  name: string;
  already_installed?: boolean;
  audits?: SkillAudits;
}
export const marketApi = {
  trending: (market: SkillMarket, timezone: string, signal?: AbortSignal) =>
    request<MarketFeed>(
      `/skill-market/${market}/trending?timezone=${encodeURIComponent(timezone)}`,
      'GET',
      undefined,
      undefined,
      signal,
    ),
  refresh: (market: SkillMarket, timezone: string) =>
    request<MarketFeed>(`/skill-market/${market}/refresh`, 'POST', { timezone }),
  detail: (market: SkillMarket, id: string, signal?: AbortSignal) =>
    request<MarketSkillDetail>(
      `/skill-market/${market}/detail?id=${encodeURIComponent(id)}`,
      'GET',
      undefined,
      undefined,
      signal,
    ),
  install: (market: SkillMarket, id: string) =>
    request<MarketInstallResult>(`/skill-market/${market}/install`, 'POST', { id }),
};
