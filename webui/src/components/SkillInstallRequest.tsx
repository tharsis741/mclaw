import { useState } from 'react';
import { BookOpen, X } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { DraftFiles, ReviewValue, sourceLabel, skillBody } from './SkillsPage';
import type { PendingRequest } from '../types';

export function SkillInstallRequest({
  request,
  onReply,
  busy,
}: {
  request: PendingRequest;
  onReply: (value: unknown) => Promise<void>;
  busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const reply = (approved: boolean) =>
    void onReply({
      approved,
      ...(approved ? { confirmation_token: request.confirmation_token } : {}),
    })
      .then(() => setOpen(false))
      .catch(() => {});
  return (
    <>
      <section className="authorization-card">
        <div className="authorization-title">
          <BookOpen size={18} />
          <h2>安装技能：{request.skill_name}</h2>
          <span>等待确认</span>
        </div>
        <p>{sourceLabel(request.source)}</p>
        <div className="authorization-actions">
          <button className="text-button" disabled={busy} onClick={() => reply(false)}>
            取消安装
          </button>
          <button className="primary" onClick={() => setOpen(true)}>
            查看安装详情
          </button>
        </div>
      </section>
      {open && (
        <div className="drawer-backdrop">
          <aside
            className="detail-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="确认技能安装"
          >
            <header className="drawer-heading">
              <h2>{request.skill_name}</h2>
              <button
                className="icon-button"
                aria-label="关闭安装详情"
                onClick={() => setOpen(false)}
              >
                <X size={18} />
              </button>
            </header>
            <p className="drawer-description">{sourceLabel(request.source)}</p>
            <section className="review-panel">
              <h3>安装检查</h3>
              <ReviewValue value={request.review} />
              {request.blocked && (
                <div className="error-banner">当前检查未通过，请处理提示的问题后重新准备。</div>
              )}
            </section>
            <details className="plain-details">
              <summary>依赖与运行条件</summary>
              <ReviewValue value={request.dependency_hints} />
            </details>
            <details className="plain-details">
              <summary>技能内容</summary>
              <div className="markdown skill-markdown">
                <Markdown remarkPlugins={[remarkGfm]}>{skillBody(request.content ?? '')}</Markdown>
              </div>
            </details>
            <details className="plain-details">
              <summary>随附文件</summary>
              <DraftFiles id={request.drafting_id} files={request.files} />
            </details>
            <footer className="drawer-footer">
              <button className="text-button" disabled={busy} onClick={() => reply(false)}>
                取消安装
              </button>
              <button
                className="primary"
                disabled={busy || request.blocked || !request.confirmation_token}
                onClick={() => reply(true)}
              >
                确认安装并继续
              </button>
            </footer>
          </aside>
        </div>
      )}
    </>
  );
}
