import { memo, useCallback, useDeferredValue, useEffect, useId, useRef, useState } from 'react';
import {
  ArrowDown,
  Check,
  ChevronDown,
  Copy,
  LoaderCircle,
  Pause,
  Wrench,
  Lightbulb,
  Monitor,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Delegation, FileReference, Message, ProcessEntry, Session } from '../types';
import { AttachmentChips } from './Attachments';
import { RemoteExecution } from './RemoteExecution';
import { BrandWordmark } from './BrandWordmark';
import { ChannelLogo } from './ChannelLogo';
import { sourceLabel } from './SkillsPage';
import { STATUS_TEXT } from '../session-state';
import { Subagents } from './Subagents';
import { MessageTime } from './MessageTime';
const scrollPositions = new Map<string, number>();

export const TOOL_LABELS: Record<string, string> = {
  delegate_task: '子代理',
  terminal: '运行命令',
  process: '查看进程',
  read_file: '读取文件',
  write_file: '写入文件',
  edit_file: '修改文件',
  patch: '修改文件',
  delete_file: '删除文件',
  list_directory: '查看目录',
  search_files: '查找文件',
  web_search: '搜索网页',
  web_extract: '读取网页',
  browser_navigate: '打开网页',
  browser_snapshot: '查看网页',
  browser_click: '点击页面',
  browser_download: '下载文件',
  browser_screenshot: '网页截图',
  skill_view: '读取技能',
  dsoftbus_list_peers: '查看协作设备',
  dsoftbus_get_device_context: '读取设备能力',
  dsoftbus_run_agent_task: '远程任务',
  dsoftbus_continue_agent_task: '继续远程任务',
  dsoftbus_read_task_result: '读取远程回复',
  dsoft_bus_source_list: '查看共享目录',
  dsoft_bus_source_search: '搜索共享资料',
  dsoft_bus_source_fetch: '读取共享文件',
  return_artifact: '返回文件',
  request_task_input: '请求补充资料',
};
const PROCESS_STATUS = {
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断',
  unknown: '结果待核对',
  waiting: '等待补充',
  awaiting_confirmation: '等待确认',
};

function parseArguments(text?: string): Record<string, unknown> {
  try {
    const value = JSON.parse(text || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

function outputText(text: string) {
  try {
    const result = JSON.parse(text);
    if (result && typeof result === 'object') {
      const streams = ['output', 'stdout', 'stderr'].filter(
        (key) => typeof result[key] === 'string',
      );
      if (streams.length) {
        const output = streams
          .map((key) => `${streams.length > 1 ? key + ':\n' : ''}${result[key]}`)
          .join('\n');
        const remaining = Object.fromEntries(
          Object.entries(result).filter(([key]) => !streams.includes(key)),
        );
        return [output, Object.keys(remaining).length ? JSON.stringify(remaining, null, 2) : '']
          .filter(Boolean)
          .join('\n\n');
      }
      return JSON.stringify(result, null, 2);
    }
  } catch {
    /* Plain command output is already readable. */
  }
  return text;
}

const MARKDOWN_PLUGINS = [remarkGfm];
const MessageMarkdown = memo(function MessageMarkdown({ content }: { content: string }) {
  const text = useDeferredValue(content);
  return <Markdown remarkPlugins={MARKDOWN_PLUGINS}>{text}</Markdown>;
});

const ProgressNote = memo(function ProgressNote({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const preview = useRef<HTMLDivElement>(null);
  const contentId = useId();
  useEffect(() => {
    const node = preview.current;
    if (!node || expanded) return;
    const measure = () => {
      if (node.clientHeight > 0) setOverflowing(node.scrollHeight > node.clientHeight + 1);
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    window.addEventListener('resize', measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [content, expanded]);
  return (
    <div className="process-note">
      <div
        id={contentId}
        ref={preview}
        className={`process-note-content markdown${expanded ? '' : ' is-collapsed'}`}
      >
        <MessageMarkdown content={content} />
      </div>
      {(overflowing || expanded) && (
        <button
          className="process-note-toggle"
          aria-label={expanded ? '收起任务进展' : '展开任务进展'}
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? '收起' : '展开'}
          <ChevronDown size={12} />
        </button>
      )}
    </div>
  );
});

const ExecutionEntry = memo(function ExecutionEntry({ entry }: { entry: ProcessEntry }) {
  const [open, setOpen] = useState(false);
  if (entry.kind === 'remote') return <RemoteExecution entry={entry} />;
  if (entry.kind === 'progress') {
    return entry.content ? <ProgressNote content={entry.content} /> : null;
  }
  const tool = entry.kind === 'tool';
  const reasoning = entry.kind === 'reasoning';
  const reasoningActive = reasoning && entry.status === 'running';
  const label = tool
    ? TOOL_LABELS[entry.title] || entry.title
    : reasoningActive
      ? '思考中'
      : reasoning && entry.status === 'completed'
        ? '思考完成'
        : entry.title;
  const args = parseArguments(entry.arguments);
  const target = [args.command, args.path, args.url, args.query, args.directory].find(
    (value) => typeof value === 'string',
  ) as string | undefined;
  const command = typeof args.command === 'string' ? args.command : undefined;
  const extraArguments = command
    ? Object.fromEntries(Object.entries(args).filter(([key]) => key !== 'command'))
    : null;
  const argumentsText = extraArguments
    ? Object.keys(extraArguments).length
      ? JSON.stringify(extraArguments, null, 2)
      : undefined
    : entry.arguments;
  return (
    <details
      className={`execution-entry ${tool ? 'tool-entry' : 'reasoning-entry'}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <ChevronDown size={14} className="entry-chevron" />
        {tool ? (
          <Wrench size={14} />
        ) : reasoningActive ? (
          <LoaderCircle size={14} className="spin" />
        ) : (
          <Lightbulb size={14} />
        )}
        <span className="entry-label">{label}</span>
        {target && (
          <code className="entry-target" title={target}>
            {target}
          </code>
        )}
        {entry.status && !(reasoning && ['running', 'completed'].includes(entry.status)) && (
          <span className={`entry-status ${entry.status}`}>
            {entry.status === 'running' && <LoaderCircle size={12} className="spin" />}
            {PROCESS_STATUS[entry.status]}
          </span>
        )}
      </summary>
      {open && (
        <div className="execution-detail">
          {tool && <div className="tool-detail-name">{entry.title}</div>}
          {command && (
            <>
              <h4>命令</h4>
              <pre>{command}</pre>
            </>
          )}
          {argumentsText && (
            <>
              <h4>参数</h4>
              <pre>{argumentsText}</pre>
            </>
          )}
          {entry.output !== undefined && (
            <>
              <h4>输出</h4>
              <pre>{outputText(entry.output)}</pre>
            </>
          )}
          {entry.content && (
            <div className="markdown">
              <MessageMarkdown content={entry.content} />
            </div>
          )}
          {tool && !entry.arguments && entry.output === undefined && (
            <p className="muted">这条历史记录没有保存调用详情。</p>
          )}
          {(entry.truncated || entry.output_truncated) && (
            <p className="muted">内容较长，当前显示前 64,000 个字符。</p>
          )}
        </div>
      )}
    </details>
  );
});

function ExecutionProcess({
  entries,
  delegations,
  onFilePreview,
}: {
  entries: ProcessEntry[];
  delegations?: Record<string, Delegation>;
  onFilePreview: (file: FileReference | { id: string; name: string }) => void;
}) {
  const [open, setOpen] = useState(true);
  const contentId = useId();
  const tools = new Set(
    entries.filter((entry) => entry.kind === 'tool').map((entry) => entry.tool_call_id || entry.id),
  ).size;
  const groups = Object.entries(delegations ?? {});
  const linked = new Set(entries.map((entry) => entry.tool_call_id).filter(Boolean));
  const visible = entries.filter(
    (entry) =>
      !(
        entry.kind === 'tool' &&
        entry.tool_call_id &&
        entries.some(
          (remote) => remote.kind === 'remote' && remote.tool_call_id === entry.tool_call_id,
        )
      ),
  );
  return (
    <section className="process" aria-label="执行过程">
      <button
        className="process-heading"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronDown size={13} className="process-chevron" />
        执行过程{tools > 0 && <span>{tools} 次工具调用</span>}
      </button>
      <div id={contentId} className="process-content" hidden={!open}>
        {visible.map((entry) => {
          const group = groups.find(
            ([, value]) => value.tool_call_id && value.tool_call_id === entry.tool_call_id,
          );
          return group ? (
            <Subagents
              key={entry.id}
              identifier={group[0]}
              group={group[1]}
              onFilePreview={onFilePreview}
            />
          ) : (
            <ExecutionEntry key={entry.id} entry={entry} />
          );
        })}
        {groups
          .filter(([, group]) => !group.tool_call_id || !linked.has(group.tool_call_id))
          .map(([id, group]) => (
            <Subagents key={id} identifier={id} group={group} onFilePreview={onFilePreview} />
          ))}
      </div>
    </section>
  );
}

const MessageView = memo(function MessageView({
  message,
  onFilePreview,
}: {
  message: Message;
  onFilePreview: (file: FileReference | { id: string; name: string }) => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <article className={`message message-${message.role}`}>
      <div className="message-identity">
        {message.role === 'user' ? (
          message.source === 'weixin' || message.source === 'dingtalk' ? (
            <ChannelLogo source={message.source} size={20} />
          ) : message.source === 'delegate' ? (
            '主代理'
          ) : (
            '你'
          )
        ) : message.role === 'assistant' ? (
          <BrandWordmark />
        ) : (
          '运行记录'
        )}
        {message.mode === 'steer' && (
          <span className="message-tag">
            补充当前任务 · {message.applied ? '已应用' : '已接收'}
          </span>
        )}
        {message.invalidated && <span className="message-tag">已撤销</span>}
      </div>
      {(!!message.process?.length || !!Object.keys(message.delegations ?? {}).length) && (
        <ExecutionProcess
          entries={message.process ?? []}
          delegations={message.delegations}
          onFilePreview={onFilePreview}
        />
      )}
      <div className="markdown">
        <MessageMarkdown content={message.content} />
      </div>
      {!!message.attachments?.length && (
        <AttachmentChips files={message.attachments} onPreview={onFilePreview} />
      )}
      {message.device_selection?.mode === 'selected' && (
        <div className="message-devices" aria-label="本条任务的协作设备">
          {message.device_selection.devices.map((device) => (
            <span key={device.id}>
              <Monitor size={12} />
              {device.name}
            </span>
          ))}
        </div>
      )}
      {!!message.skills?.length && (
        <div className="message-skills">
          {message.skills.map((skill) => (
            <details key={`${skill.name}:${skill.version}`}>
              <summary>{skill.name}</summary>
              <dl className="compact-definition">
                <dt>来源</dt>
                <dd>{sourceLabel(skill.source, true)}</dd>
                <dt>版本</dt>
                <dd>{skill.version || '未记录'}</dd>
              </dl>
            </details>
          ))}
        </div>
      )}
      {(message.role === 'user' || message.role === 'assistant') && (
        <div className="message-footer">
          <MessageTime value={message.created_at} />
          {message.role === 'assistant' && !!message.content && (
            <button
              className="copy-button"
              aria-label="复制回复"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(message.content);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                } catch {
                  setCopied(false);
                }
              }}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
              {copied ? '已复制' : '复制'}
            </button>
          )}
        </div>
      )}
    </article>
  );
});

export function Conversation({
  session,
  onFilePreview,
}: {
  session: Session;
  onFilePreview?: (file: FileReference | { id: string; name: string }) => void;
}) {
  const previewCallback = useRef(onFilePreview);
  previewCallback.current = onFilePreview;
  const previewFile = useCallback(
    (file: FileReference | { id: string; name: string }) => previewCallback.current?.(file),
    [],
  );
  const scroller = useRef<HTMLDivElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  useEffect(() => {
    const node = scroller.current;
    if (node) {
      node.scrollTop = scrollPositions.get(session.id) ?? node.scrollHeight;
      atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
      setShowLatest(!atBottom.current);
    }
    return () => {
      if (node) scrollPositions.set(session.id, node.scrollTop);
    };
  }, [session.id]);
  useEffect(() => {
    if (atBottom.current) end.current?.scrollIntoView({ block: 'end' });
  }, [session.messages]);
  return (
    <div className="conversation-frame">
      <div
        className="conversation"
        ref={scroller}
        onScroll={(event) => {
          const node = event.currentTarget;
          atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
          scrollPositions.set(session.id, node.scrollTop);
          setShowLatest(!atBottom.current);
        }}
      >
        <div className="message-list">
          {session.messages?.map((message) => (
            <MessageView
              key={`${session.id}:${message.id}`}
              message={message}
              onFilePreview={previewFile}
            />
          ))}
          {['stopping', 'stopped', 'interrupted', 'paused'].includes(session.status) && (
            <div className="run-progress" role="status">
              <Pause size={15} />
              {STATUS_TEXT[session.status]}
              {session.status !== 'stopping' && '，可以在输入区继续处理。'}
            </div>
          )}
          <div ref={end} />
        </div>
      </div>
      {showLatest && (
        <button
          className="latest-button"
          onClick={() => {
            atBottom.current = true;
            end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
          }}
        >
          <ArrowDown size={14} />
          回到最新
        </button>
      )}
    </div>
  );
}
