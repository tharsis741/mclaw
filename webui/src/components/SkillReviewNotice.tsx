import { ArrowUpRight, CheckCircle2, ChevronDown, CircleAlert } from 'lucide-react';
import type { SkillReviewNotice as Notice } from '../types';
import { MessageTime } from './MessageTime';

const actionLabels: Record<string, string> = {
  create: '创建技能',
  edit: '更新技能正文',
  patch: '修改技能正文',
  evolution_update: '更新经验',
  write_file: '保存辅助文件',
  remove_file: '删除辅助文件',
  delete: '删除技能',
};

export function SkillReviewNotice({
  notice,
  onOpenSkill,
}: {
  notice: Notice;
  onOpenSkill?: (name: string) => void;
}) {
  const incomplete = ['partial', 'failed', 'interrupted'].includes(notice.status);
  const created = notice.skills.filter(
    (item) => !item.deleted && item.actions.includes('create'),
  ).length;
  const deleted = notice.skills.filter((item) => item.deleted).length;
  const updated = notice.skills.length - created - deleted;
  const summary = [
    created && `新建 ${created} 项`,
    updated && `更新 ${updated} 项`,
    deleted && `删除 ${deleted} 项`,
  ]
    .filter(Boolean)
    .join('、');
  return (
    <article
      className={`message skill-review-notice ${incomplete ? 'skill-review-incomplete' : ''}`}
    >
      <details>
        <summary className="skill-review-summary">
          <ChevronDown size={13} className="skill-review-chevron" />
          {incomplete ? <CircleAlert size={15} /> : <CheckCircle2 size={15} />}
          <span className="skill-review-title">
            {!notice.skills.length
              ? '技能整理未完成'
              : incomplete
                ? '技能已保存，后续整理未完成'
                : '技能已保存'}
          </span>
          <span className="skill-review-count">{summary}</span>
          {notice.status === 'running' && <span className="skill-review-count">后续整理中</span>}
          <span className="skill-review-toggle">
            <span>查看</span>
            <span>收起</span>
          </span>
        </summary>
        <div className="skill-review-details">
          {notice.skills.map((item) => (
            <div className="skill-review-item" key={item.name}>
              <div className="skill-review-name">
                <span className="message-tag">
                  {item.deleted ? '删除' : item.actions.includes('create') ? '新建' : '更新'}
                </span>
                <span>{item.description || item.name}</span>
              </div>
              {item.description && <div className="skill-review-slug">{item.name}</div>}
              <div className="skill-review-actions">
                {item.actions.map((action) => actionLabels[action] || action).join(' · ')}
              </div>
              {!item.deleted && onOpenSkill && (
                <button
                  className="text-button skill-review-link"
                  onClick={() => onOpenSkill(item.name)}
                >
                  查看技能
                  <ArrowUpRight size={13} />
                </button>
              )}
            </div>
          ))}
          {incomplete && (
            <p className="skill-review-error">
              {notice.error || '后台整理未能完成。'}
              {notice.skills.length > 0 && ' 已保存的记录保留。'}
            </p>
          )}
          <div className="skill-review-footer">
            <span>后台自动整理</span>
            <MessageTime value={notice.completed_at || notice.updated_at || notice.started_at} />
          </div>
        </div>
      </details>
    </article>
  );
}
