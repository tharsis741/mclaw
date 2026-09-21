import { ChevronDown, LoaderCircle, Monitor } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ProcessEntry } from '../types';

const STATUS: Record<string, string> = {
  running: '执行中',
  completed: '已完成',
  waiting: '等待补充',
  failed: '失败',
  interrupted: '已中断',
  unknown: '结果待核对',
};

export function RemoteExecution({ entry }: { entry: ProcessEntry }) {
  let result: Record<string, unknown> = {};
  try {
    const decoded: unknown = JSON.parse(entry.output || '{}');
    if (decoded && typeof decoded === 'object' && !Array.isArray(decoded))
      result = decoded as Record<string, unknown>;
  } catch {
    /* Preserve raw details below. */
  }
  return (
    <details className="execution-entry remote-entry">
      <summary>
        <ChevronDown size={14} className="entry-chevron" />
        <Monitor size={14} />
        <span className="entry-label">{entry.title}</span>
        <span className="entry-target">{entry.device_name || '协作设备'}</span>
        <span className={`entry-status ${entry.status}`}>
          {entry.status === 'running' && <LoaderCircle size={12} className="spin" />}
          {entry.continued ? '已补充' : STATUS[entry.status || 'unknown']}
        </span>
      </summary>
      <div className="execution-detail remote-detail">
        {!!entry.progress?.length && (
          <div className="remote-progress">
            {entry.progress.map((item) => (
              <div className="markdown" key={item.id}>
                <Markdown remarkPlugins={[remarkGfm]}>{item.content}</Markdown>
              </div>
            ))}
          </div>
        )}
        {typeof result.text === 'string' && (
          <div className="markdown">
            <Markdown remarkPlugins={[remarkGfm]}>{result.text}</Markdown>
          </div>
        )}
        {entry.input_request && !entry.continued && (
          <div className="remote-input-request">
            <strong>对端需要补充资料</strong>
            {typeof entry.input_request.message === 'string' && (
              <p>{entry.input_request.message}</p>
            )}
            <p className="muted">在输入框补充文字或文件后发送，即可继续这项任务。</p>
          </div>
        )}
        {entry.status === 'unknown' && (
          <p className="remote-warning">尚未确认对端是否结束，请核对执行结果后再继续。</p>
        )}
        {entry.warnings?.map((warning) => (
          <p className="remote-warning" key={warning}>
            {warning}
          </p>
        ))}
        <details className="remote-technical">
          <summary>调用详情</summary>
          {entry.task_id && (
            <p>
              任务：<code>{entry.task_id}</code>
            </p>
          )}
          {entry.arguments && (
            <>
              <h4>参数</h4>
              <pre>{entry.arguments}</pre>
            </>
          )}
          {entry.output && (
            <>
              <h4>返回记录</h4>
              <pre>{entry.output}</pre>
            </>
          )}
          {entry.output_truncated && <p>返回记录较长，当前显示前 64,000 个字符。</p>}
        </details>
      </div>
    </details>
  );
}
