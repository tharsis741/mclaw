import { ArrowUpRight, CheckCircle2, CircleHelp, TriangleAlert } from 'lucide-react';
import type { SkillAudits } from '../skill-market-api';

export function readAudits(value: unknown): SkillAudits | null {
  if (!value || typeof value !== 'object') return null;
  const audits = (value as { audits?: SkillAudits }).audits;
  return audits && Array.isArray(audits.reports) ? audits : null;
}

export function SecurityAudits({
  audits,
  showTitle = true,
}: {
  audits: SkillAudits;
  showTitle?: boolean;
}) {
  const warning = audits.reports.some((report) => report.status === 'warn');
  const dates = audits.reports
    .map((report) => report.analyzed_at)
    .filter(Boolean)
    .sort();
  const date = dates.at(-1);
  return (
    <section className="security-audits" aria-label="安全审核">
      {showTitle && <h3>安全审核</h3>}
      {audits.reports.map((report) => (
        <div className={`audit-row audit-${report.status}`} key={report.provider}>
          {report.status === 'pass' ? (
            <CheckCircle2 size={19} />
          ) : report.status === 'warn' ? (
            <TriangleAlert size={19} />
          ) : (
            <CircleHelp size={19} />
          )}
          <span className="audit-label" title={`来源：${report.provider}`}>
            {report.label}
          </span>
          <span className="audit-state">{report.status_text}</span>
          {report.status !== 'unknown' && report.report_url && (
            <a
              href={report.report_url}
              target="_blank"
              rel="noreferrer"
              aria-label={`查看${report.label}报告`}
            >
              报告 <ArrowUpRight size={14} />
            </a>
          )}
        </div>
      ))}
      <p className={`audit-notice ${warning ? 'has-warning' : ''}`}>
        {audits.notice || '暂无审核报告。'}
      </p>
      {audits.source && (
        <p className="audit-source">
          来源：{audits.source}
          {date && ` · 审核时间 ${new Date(date).toLocaleDateString('zh-CN')}`}
        </p>
      )}
    </section>
  );
}
