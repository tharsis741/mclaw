import { Select } from './Select';
import { useCallback, useEffect, useState } from 'react';
import {
  Check,
  ChevronRight,
  Clock3,
  Copy,
  Link,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { schedulesApi } from '../api';
import type {
  ScheduledJob,
  ScheduleForm,
  SchedulePairing,
  ScheduleRun,
  ScheduleTarget,
  ModelOption,
  ScheduleDefaults,
} from '../types';

const STATUS: Record<string, string> = {
  queued: '排队中',
  pending: '等待执行',
  running: '执行中',
  completed: '已完成',
  succeeded: '已完成',
  success: '成功',
  failed: '失败',
  cancelled: '已取消',
  cancelling: '正在取消',
  skipped: '已跳过',
  enabled: '已启用',
  disabled: '已暂停',
  paused: '已暂停',
};
const RUNNING = new Set(['pending', 'queued', 'running', 'cancelling']);
const targetLabels: Record<string, string> = {
  local: '本机',
  dingtalk_group: '钉钉群聊',
  dingtalk_private: '钉钉私聊',
  weixin_private: '微信',
};
const scheduleLabel = (value = '') =>
  value
    .replace(
      /Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday/g,
      (day) =>
        ({
          Monday: '一',
          Tuesday: '二',
          Wednesday: '三',
          Thursday: '四',
          Friday: '五',
          Saturday: '六',
          Sunday: '日',
        })[day]!,
    )
    .replace(/每周\s+([一二三四五六日])/, '每周$1');
function DeliveryResult({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object' || !Object.keys(value).length) return null;
  const result = value as Record<string, unknown>;
  const status =
    (
      {
        local: '已保存到本机',
        sent: '已发送',
        failed: '投递失败',
        disabled: '投递位置已停用',
        needs_route: '请重新绑定投递位置',
        target_missing: '投递位置不存在',
        invalid_delivery: '投递设置有误',
      } as Record<string, string>
    )[String(result.status)] ?? (result.success ? '投递成功' : '未完成投递');
  return (
    <details className="plain-details">
      <summary>投递结果 · {status}</summary>
      <p>{targetLabels[String(result.target_type)] ?? '消息渠道'}</p>
      {!!result.error && <p className="inline-error">{String(result.error)}</p>}
      {typeof result.output_path === 'string' && <p>{result.output_path}</p>}
      {Array.isArray(result.attempts) && (
        <p className="muted">尝试投递 {result.attempts.length} 次</p>
      )}
    </details>
  );
}
const date = (value?: number, timeZone?: string) =>
  value
    ? new Date(value * 1000).toLocaleString('zh-CN', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        ...(timeZone ? { timeZone } : {}),
      })
    : '—';
const newForm = (defaults?: ScheduleDefaults): ScheduleForm => ({
  name: '',
  prompt: '',
  schedule: {
    trigger_type: 'daily',
    timezone: defaults?.timezone ?? 'Asia/Shanghai',
    time: '09:00',
  },
  enabled: true,
  session_policy: 'task_thread',
  concurrency_policy: 'skip',
  max_iterations: defaults?.max_iterations ?? 200,
  timeout_seconds: defaults?.timeout_seconds ?? 3600,
  delivery: { target_id: 'local', retry_count: 3 },
});

function ScheduleEditor({
  job,
  defaults,
  targets,
  models,
  onSave,
  onClose,
}: {
  job: ScheduledJob | null;
  defaults?: ScheduleDefaults;
  targets: ScheduleTarget[];
  models: ModelOption[];
  onSave: () => Promise<void>;
  onClose: () => void;
}) {
  const [form, setForm] = useState<ScheduleForm>(() => {
    if (!job) return newForm(defaults);
    const parsed = job.schedule.parsed ?? {};
    const schedule = { ...job.schedule };
    if (['daily', 'weekly', 'monthly'].includes(schedule.trigger_type))
      schedule.time =
        schedule.time ??
        `${String(parsed.hour ?? 9).padStart(2, '0')}:${String(parsed.minute ?? 0).padStart(2, '0')}`;
    if (schedule.trigger_type === 'weekly')
      schedule.weekday = Number(parsed.weekday ?? schedule.weekday ?? 0);
    if (schedule.trigger_type === 'monthly') schedule.day = Number(parsed.day ?? schedule.day ?? 1);
    if (schedule.trigger_type === 'once')
      schedule.at = String(parsed.at ?? schedule.at ?? schedule.expression ?? '').slice(0, 16);
    if (schedule.trigger_type === 'interval')
      schedule.every = schedule.every ?? schedule.expression;
    return {
      name: job.name,
      prompt: job.prompt,
      schedule,
      model: job.model,
      workdir: job.workdir,
      enabled: job.enabled,
      session_policy: job.session_policy,
      concurrency_policy: job.concurrency_policy,
      enabled_toolsets: job.enabled_toolsets,
      max_iterations: job.max_iterations,
      timeout_seconds: job.timeout_seconds,
      delivery: job.delivery,
    };
  });
  const [preview, setPreview] = useState<{ next_runs: number[]; schedule_label: string } | null>(
    job?.next_runs ? { next_runs: job.next_runs, schedule_label: job.schedule_label ?? '' } : null,
  );
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const schedule = form.schedule;
  const changeSchedule = (changes: Partial<ScheduleForm['schedule']>) => {
    setForm((current) => ({ ...current, schedule: { ...current.schedule, ...changes } }));
    setPreview(null);
  };
  const showPreview = async () => {
    setBusy(true);
    setError('');
    try {
      setPreview(await schedulesApi.preview(schedule));
    } catch (e) {
      setError(e instanceof Error ? e.message : '时间设置有误');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="detail-drawer schedule-editor"
        role="dialog"
        aria-modal="true"
        aria-label={job ? '编辑定时任务' : '新建定时任务'}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="drawer-heading">
          <h2>{job ? '编辑定时任务' : '新建定时任务'}</h2>
          <button className="icon-button" aria-label="关闭任务编辑" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError('');
            try {
              if (job) await schedulesApi.update(job.id, form);
              else await schedulesApi.create(form);
              await onSave();
              onClose();
            } catch (e) {
              setError(e instanceof Error ? e.message : '保存失败');
            } finally {
              setBusy(false);
            }
          }}
        >
          {error && (
            <div className="error-banner" role="alert">
              {error}
            </div>
          )}
          <label className="field-label">
            任务名称
            <input
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="例如：整理每周工作摘要"
            />
          </label>
          <label className="field-label">
            要做什么
            <textarea
              required
              rows={5}
              value={form.prompt}
              onChange={(e) => setForm({ ...form, prompt: e.target.value })}
              placeholder="描述要执行的任务和期望的结果"
            />
          </label>
          <div className="form-grid">
            <label className="field-label">
              重复
              <Select
                label="重复"
                value={schedule.trigger_type}
                onChange={(selectedValue) => {
                  const trigger_type = selectedValue as typeof schedule.trigger_type;
                  setForm({
                    ...form,
                    schedule: {
                      trigger_type,
                      timezone: schedule.timezone,
                      ...(trigger_type === 'once'
                        ? { at: '' }
                        : trigger_type === 'interval'
                          ? { every: '30m' }
                          : trigger_type === 'cron'
                            ? { expression: '0 9 * * *' }
                            : {
                                time: '09:00',
                                ...(trigger_type === 'weekly' ? { weekday: 0 } : {}),
                                ...(trigger_type === 'monthly' ? { day: 1 } : {}),
                              }),
                    },
                  });
                  setPreview(null);
                }}
              >
                <option value="once">仅一次</option>
                <option value="daily">每天</option>
                <option value="weekly">每周</option>
                <option value="monthly">每月</option>
                <option value="interval">固定间隔</option>
                <option value="cron">自定义 Cron</option>
              </Select>
            </label>
            {schedule.trigger_type === 'once' ? (
              <label className="field-label">
                执行时间
                <input
                  required
                  type="datetime-local"
                  value={schedule.at ?? ''}
                  onChange={(e) => changeSchedule({ at: e.target.value })}
                />
              </label>
            ) : schedule.trigger_type === 'interval' ? (
              <label className="field-label">
                间隔
                <input
                  required
                  value={schedule.every ?? ''}
                  placeholder="例如 30m、2h"
                  onChange={(e) => changeSchedule({ every: e.target.value })}
                />
              </label>
            ) : schedule.trigger_type === 'cron' ? (
              <label className="field-label">
                Cron 表达式
                <input
                  required
                  value={schedule.expression ?? ''}
                  onChange={(e) => changeSchedule({ expression: e.target.value })}
                />
              </label>
            ) : (
              <label className="field-label">
                时间
                <input
                  required
                  type="time"
                  value={schedule.time ?? '09:00'}
                  onChange={(e) => changeSchedule({ time: e.target.value })}
                />
              </label>
            )}
            {schedule.trigger_type === 'weekly' && (
              <label className="field-label">
                星期
                <Select
                  label="星期"
                  value={schedule.weekday ?? 0}
                  onChange={(selectedValue) => changeSchedule({ weekday: Number(selectedValue) })}
                >
                  {['一', '二', '三', '四', '五', '六', '日'].map((day, index) => (
                    <option key={day} value={index}>
                      星期{day}
                    </option>
                  ))}
                </Select>
              </label>
            )}
            {schedule.trigger_type === 'monthly' && (
              <label className="field-label">
                日期
                <input
                  type="number"
                  min={1}
                  max={31}
                  value={schedule.day ?? 1}
                  onChange={(e) => changeSchedule({ day: Number(e.target.value) })}
                />
              </label>
            )}
            <label className="field-label">
              时区
              <Select
                label="时区"
                editable
                required
                value={schedule.timezone}
                onChange={(value) => changeSchedule({ timezone: value })}
              >
                <option value="Asia/Shanghai" />
                <option value="UTC" />
                <option value="Asia/Hong_Kong" />
              </Select>
            </label>
          </div>
          <div className="schedule-preview">
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() => void showPreview()}
            >
              <Clock3 size={14} />
              预览接下来 3 次
            </button>
            {preview && (
              <>
                <p>{scheduleLabel(preview.schedule_label)}</p>
                <ol>
                  {preview.next_runs.map((time) => (
                    <li key={time}>{date(time, schedule.timezone)}</li>
                  ))}
                </ol>
                {!preview.next_runs.length && <p>没有后续执行时间，请检查时间设置。</p>}
              </>
            )}
          </div>
          <details className="plain-details">
            <summary>工作区、执行方式与投递</summary>
            <label className="field-label">
              模型
              <Select
                label="模型"
                searchable
                value={form.model ?? ''}
                onChange={(selectedValue) => setForm({ ...form, model: selectedValue })}
              >
                <option value="">使用默认模型</option>
                {models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.label}
                  </option>
                ))}
              </Select>
            </label>
            <label className="field-label">
              工作目录
              <input
                value={form.workdir ?? ''}
                onChange={(e) => setForm({ ...form, workdir: e.target.value })}
                placeholder="留空使用默认目录"
              />
            </label>
            <div className="form-grid">
              <label className="field-label">
                会话方式
                <Select
                  label="会话方式"
                  value={form.session_policy ?? 'task_thread'}
                  onChange={(selectedValue) =>
                    setForm({
                      ...form,
                      session_policy: selectedValue as ScheduleForm['session_policy'],
                      concurrency_policy:
                        selectedValue === 'task_thread' && form.concurrency_policy === 'parallel'
                          ? 'skip'
                          : form.concurrency_policy,
                    })
                  }
                >
                  <option value="task_thread">沿用此计划的会话</option>
                  <option value="new_session">每次新建会话</option>
                </Select>
              </label>
              <label className="field-label">
                上一次还没完成时
                <Select
                  label="上一次还没完成时"
                  value={form.concurrency_policy ?? 'skip'}
                  onChange={(selectedValue) =>
                    setForm({
                      ...form,
                      concurrency_policy: selectedValue as ScheduleForm['concurrency_policy'],
                    })
                  }
                >
                  <option value="skip">跳过本次</option>
                  <option value="queue">排队等待</option>
                  <option value="parallel" disabled={form.session_policy !== 'new_session'}>
                    同时执行（需每次新建会话）
                  </option>
                  <option value="replace">停止上次，执行本次</option>
                </Select>
              </label>
              <label className="field-label">
                最长运行时间（秒）
                <input
                  type="number"
                  min={1}
                  value={form.timeout_seconds ?? 3600}
                  onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })}
                />
              </label>
              <label className="field-label">
                最多执行轮数
                <input
                  type="number"
                  min={1}
                  value={form.max_iterations ?? 200}
                  onChange={(e) => setForm({ ...form, max_iterations: Number(e.target.value) })}
                />
              </label>
            </div>
            <label className="field-label">
              结果投递到
              <Select
                label="结果投递到"
                value={form.delivery?.target_id ?? 'local'}
                onChange={(selectedValue) =>
                  setForm({ ...form, delivery: { ...form.delivery, target_id: selectedValue } })
                }
              >
                <option value="local">本机保存</option>
                {targets
                  .filter((target) => target.id !== 'local')
                  .map((target) => (
                    <option
                      key={target.id}
                      value={target.id}
                      disabled={!target.enabled || target.route_status !== 'ready'}
                    >
                      {target.display_name}
                      {target.route_status !== 'ready' ? '（未就绪）' : ''}
                    </option>
                  ))}
              </Select>
            </label>
            <label className="field-label">
              结果保存位置（可选）
              <input
                value={form.delivery?.final_response_path ?? ''}
                onChange={(e) =>
                  setForm({
                    ...form,
                    delivery: {
                      target_id: form.delivery?.target_id ?? 'local',
                      ...form.delivery,
                      final_response_path: e.target.value,
                    },
                  })
                }
              />
            </label>
          </details>
          <label className="check-field">
            <input
              type="checkbox"
              checked={form.enabled !== false}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
            保存后启用
          </label>
          <footer className="drawer-footer">
            <button type="button" className="text-button" onClick={onClose}>
              取消
            </button>
            <button className="primary" disabled={busy || !form.name.trim() || !form.prompt.trim()}>
              <Check size={15} />
              保存任务
            </button>
          </footer>
        </form>
      </aside>
    </div>
  );
}

export function SchedulesPage({
  onSession,
  models = [],
}: {
  onSession: (id: string) => void;
  models?: ModelOption[];
}) {
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [total, setTotal] = useState(0);
  const [defaults, setDefaults] = useState<ScheduleDefaults>();
  const [targets, setTargets] = useState<ScheduleTarget[]>([]);
  const [runtime, setRuntime] = useState<{ running: boolean; reason?: string } | null>(null);
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState<'jobs' | 'targets'>('jobs');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<ScheduledJob | 'new' | null>(null);
  const [selected, setSelected] = useState<ScheduledJob | null>(null);
  const [runs, setRuns] = useState<ScheduleRun[]>([]);
  const [moreRuns, setMoreRuns] = useState(false);
  const [deleteItem, setDeleteItem] = useState<{
    kind: 'job' | 'target';
    id: string;
    name: string;
  } | null>(null);
  const [pairing, setPairing] = useState<SchedulePairing | null>(null);
  const [pairOpen, setPairOpen] = useState(false);
  const [pairType, setPairType] = useState('weixin_private');
  const [pairName, setPairName] = useState('');
  const [rename, setRename] = useState<ScheduleTarget | null>(null);
  const load = useCallback(async () => {
    const results = await Promise.allSettled([schedulesApi.list(), schedulesApi.targets()]);
    results.forEach((result) => {
      if (result.status === 'rejected')
        setError(result.reason instanceof Error ? result.reason.message : '加载失败');
    });
    if (results[0].status === 'fulfilled') {
      setJobs(results[0].value.items);
      setTotal(results[0].value.total);
      setRuntime(results[0].value.runtime);
      setDefaults(results[0].value.defaults);
    }
    if (results[1].status === 'fulfilled') setTargets(results[1].value.items);
    setLoading(false);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    if (!selected) return;
    let active = true;
    let first = true;
    setRuns([]);
    setMoreRuns(false);
    const poll = () =>
      void schedulesApi
        .runs(selected.id)
        .then((result) => {
          if (active) {
            const ids = new Set(result.items.map((item) => item.id));
            setRuns((current) =>
              first
                ? result.items
                : [...result.items, ...current.filter((item) => !ids.has(item.id))],
            );
            if (first) setMoreRuns(result.items.length === 30);
            first = false;
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    poll();
    const interval = setInterval(poll, 4000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [selected]);
  useEffect(() => {
    if (
      !pairing ||
      !pairOpen ||
      ['bound', 'completed', 'expired', 'cancelled', 'failed'].includes(pairing.status)
    )
      return;
    const interval = setInterval(() => {
      void schedulesApi
        .pairing(pairing.code)
        .then((next) => {
          setPairing(next);
          if (next.target_id) void load();
        })
        .catch((e) => setError(e.message));
    }, 2000);
    return () => clearInterval(interval);
  }, [pairing, pairOpen, load]);
  return (
    <section className="management-page">
      <div className="page-heading">
        <div>
          <h2>定时任务</h2>
        </div>
        <button
          className="primary"
          disabled={busy}
          onClick={() =>
            void action(async () => {
              const result = await schedulesApi.list();
              setDefaults(result.defaults);
              setEditor('new');
            })
          }
        >
          <Plus size={15} />
          新建任务
        </button>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      {runtime && !runtime.running && (
        <div className="notice">{runtime.reason || '定时执行暂未启动，已保存的任务会保留。'}</div>
      )}
      <nav className="page-tabs">
        <button className={tab === 'jobs' ? 'active' : ''} onClick={() => setTab('jobs')}>
          任务 <span>{total}</span>
        </button>
        <button className={tab === 'targets' ? 'active' : ''} onClick={() => setTab('targets')}>
          投递位置 <span>{targets.length}</span>
        </button>
      </nav>
      {tab === 'jobs' ? (
        <>
          <div className="table-toolbar">
            <label className="table-search">
              <Search size={15} />
              <input
                aria-label="搜索定时任务"
                value={query}
                placeholder="搜索名称或任务内容"
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <button className="icon-button" aria-label="刷新定时任务" onClick={() => void load()}>
              <RefreshCw size={15} />
            </button>
          </div>
          <div className="data-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>时间</th>
                  <th>下次运行</th>
                  <th>状态</th>
                  <th aria-label="操作" />
                </tr>
              </thead>
              <tbody>
                {jobs
                  .filter((job) =>
                    `${job.name} ${job.prompt}`.toLowerCase().includes(query.toLowerCase()),
                  )
                  .map((job) => (
                    <tr key={job.id}>
                      <td>
                        <button className="row-title" onClick={() => setSelected(job)}>
                          <Clock3 size={17} />
                          <span>
                            <strong>{job.name}</strong>
                            <small>{job.prompt}</small>
                          </span>
                        </button>
                      </td>
                      <td>
                        {scheduleLabel(job.schedule_label) || job.schedule.time || '自定义时间'}
                      </td>
                      <td>{job.enabled ? date(job.next_run_at, job.schedule.timezone) : '—'}</td>
                      <td>
                        <button
                          className={`switch ${job.enabled ? 'on' : ''}`}
                          role="switch"
                          aria-checked={!!job.enabled}
                          aria-label={`${job.name} 启用状态`}
                          disabled={busy}
                          onClick={() =>
                            void action(async () => {
                              await schedulesApi.enabled(job.id, !job.enabled);
                              await load();
                            })
                          }
                        >
                          <span />
                        </button>
                        <span className="table-status">{job.enabled ? '已启用' : '已暂停'}</span>
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="icon-button"
                            aria-label={`立即运行 ${job.name}`}
                            title="立即运行"
                            disabled={busy}
                            onClick={() =>
                              void action(async () => {
                                const run = await schedulesApi.run(job.id);
                                setSelected(job);
                                setRuns([run]);
                                setNotice('已提交运行请求。');
                              })
                            }
                          >
                            <Play size={15} />
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`编辑 ${job.name}`}
                            title="编辑"
                            onClick={() => setEditor(job)}
                          >
                            <Pencil size={15} />
                          </button>
                          <button
                            className="icon-button"
                            aria-label={`删除 ${job.name}`}
                            title="删除"
                            onClick={() =>
                              setDeleteItem({ kind: 'job', id: job.id, name: job.name })
                            }
                          >
                            <Trash2 size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
            {loading ? (
              <p className="list-empty">正在读取任务…</p>
            ) : (
              !jobs.length && (
                <p className="list-empty">还没有定时任务。可以先添加一个每日提醒或定期整理任务。</p>
              )
            )}
          </div>
          {jobs.length < total && (
            <button
              className="load-more text-button"
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  const result = await schedulesApi.list(jobs.length);
                  setJobs((current) => [...current, ...result.items]);
                })
              }
            >
              加载更多任务
            </button>
          )}
        </>
      ) : (
        <>
          <div className="table-toolbar">
            <span>把执行结果发送到常用的消息渠道。</span>
            <button
              className="secondary"
              onClick={() => {
                setPairOpen(true);
                setPairing(null);
              }}
            >
              <Link size={14} />
              绑定投递位置
            </button>
          </div>
          <div className="data-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>渠道</th>
                  <th>状态</th>
                  <th aria-label="操作" />
                </tr>
              </thead>
              <tbody>
                {targets.map((target) => (
                  <tr key={target.id}>
                    <td>
                      <button className="row-title" onClick={() => setRename(target)}>
                        <strong>{target.display_name}</strong>
                      </button>
                    </td>
                    <td>{targetLabels[target.type] ?? '消息渠道'}</td>
                    <td>
                      {target.route_status === 'ready'
                        ? '就绪'
                        : target.route_status === 'disabled'
                          ? '已停用'
                          : '待绑定'}
                    </td>
                    <td>
                      <div className="row-actions">
                        {target.type !== 'local' && (
                          <>
                            <button
                              className="text-button"
                              disabled={busy || target.route_status !== 'ready'}
                              onClick={() =>
                                void action(async () => {
                                  const result = await schedulesApi.testTarget(target.id);
                                  if (!result.success)
                                    throw new Error(result.error || '测试消息发送失败');
                                  setNotice('测试消息已发送。');
                                })
                              }
                            >
                              发送测试消息
                            </button>
                            <button
                              className="text-button"
                              disabled={busy}
                              onClick={() =>
                                void action(async () => {
                                  await schedulesApi.updateTarget(target.id, {
                                    enabled: !target.enabled,
                                  });
                                  await load();
                                })
                              }
                            >
                              {target.enabled ? '停用' : '启用'}
                            </button>
                            <button
                              className="icon-button"
                              aria-label={`删除投递位置 ${target.display_name}`}
                              onClick={() =>
                                setDeleteItem({
                                  kind: 'target',
                                  id: target.id,
                                  name: target.display_name,
                                })
                              }
                            >
                              <Trash2 size={15} />
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {editor && (
        <ScheduleEditor
          defaults={defaults}
          models={models}
          job={editor === 'new' ? null : editor}
          targets={targets}
          onSave={load}
          onClose={() => setEditor(null)}
        />
      )}
      {selected && (
        <div className="drawer-backdrop" onClick={() => setSelected(null)}>
          <aside
            className="detail-drawer"
            role="dialog"
            aria-modal="true"
            aria-label={`${selected.name} 运行记录`}
            onClick={(e) => e.stopPropagation()}
          >
            <header className="drawer-heading">
              <div>
                <h2>{selected.name}</h2>
              </div>
              <button
                className="icon-button"
                aria-label="关闭运行记录"
                onClick={() => setSelected(null)}
              >
                <X size={18} />
              </button>
            </header>
            <p className="drawer-description">
              {scheduleLabel(selected.schedule_label) || selected.schedule.timezone}
            </p>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            <h3 className="section-label">运行记录</h3>
            {!runs.length && <p className="list-empty">还没有运行记录。</p>}
            {runs.map((run) => (
              <details className="run-record" key={run.id}>
                <summary>
                  <span className={`run-status status-${run.status}`}>
                    {STATUS[run.status] ?? '处理中'}
                  </span>
                  <span>
                    {date(run.started_at ?? run.scheduled_for, selected.schedule.timezone)}
                  </span>
                  <ChevronRight size={14} />
                </summary>
                <div className="run-record-body">
                  <dl className="compact-definition">
                    <dt>计划时间</dt>
                    <dd>{date(run.scheduled_for, selected.schedule.timezone)}</dd>
                    <dt>完成时间</dt>
                    <dd>{date(run.finished_at, selected.schedule.timezone)}</dd>
                    {run.output_path && (
                      <>
                        <dt>结果文件</dt>
                        <dd>{run.output_path}</dd>
                      </>
                    )}
                  </dl>
                  {run.error && <p className="inline-error">{run.error}</p>}
                  {run.final_response && (
                    <div className="markdown skill-markdown">
                      <Markdown remarkPlugins={[remarkGfm]}>{run.final_response}</Markdown>
                    </div>
                  )}
                  <DeliveryResult value={run.delivery_result} />
                  {Array.isArray(run.tool_calls) && !!run.tool_calls.length && (
                    <p className="muted">
                      使用工具 {run.tool_calls.length} 次 · 完整过程可在会话中查看
                    </p>
                  )}
                  <div className="row-actions">
                    {run.session_id && (
                      <button className="text-button" onClick={() => onSession(run.session_id!)}>
                        查看会话
                        <ChevronRight size={14} />
                      </button>
                    )}
                    {RUNNING.has(run.status) && (
                      <button
                        className="text-button danger-text"
                        disabled={busy}
                        onClick={() =>
                          void action(async () => {
                            const result = await schedulesApi.cancelRun(run.id);
                            setRuns((current) =>
                              current.map((item) => (item.id === run.id ? result : item)),
                            );
                          })
                        }
                      >
                        <Pause size={14} />
                        取消本次运行
                      </button>
                    )}
                  </div>
                </div>
              </details>
            ))}
            {moreRuns && (
              <button
                className="load-more"
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    const result = await schedulesApi.runs(selected.id, runs.length);
                    setRuns((current) => {
                      const ids = new Set(current.map((item) => item.id));
                      return [...current, ...result.items.filter((item) => !ids.has(item.id))];
                    });
                    setMoreRuns(result.items.length === 30);
                  })
                }
              >
                更早的运行记录
              </button>
            )}
          </aside>
        </div>
      )}
      {deleteItem && (
        <div className="modal-backdrop">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-schedule-title"
          >
            <h2 id="delete-schedule-title">
              删除{deleteItem.kind === 'job' ? '任务' : '投递位置'}？
            </h2>
            <p>{deleteItem.name}</p>
            <p className="muted">
              {deleteItem.kind === 'job'
                ? '此计划之后将不再自动运行。'
                : '使用这个位置的任务需要重新选择投递位置。'}
            </p>
            <div className="dialog-actions">
              <button className="text-button" onClick={() => setDeleteItem(null)}>
                取消
              </button>
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    if (deleteItem.kind === 'job') await schedulesApi.remove(deleteItem.id);
                    else await schedulesApi.removeTarget(deleteItem.id);
                    setDeleteItem(null);
                    await load();
                  })
                }
              >
                确认删除
              </button>
            </div>
          </section>
        </div>
      )}
      {rename && (
        <div className="modal-backdrop">
          <section className="modal" role="dialog" aria-modal="true" aria-label="投递位置名称">
            <h2>投递位置名称</h2>
            <label className="field-label">
              名称
              <input
                value={rename.display_name}
                onChange={(e) => setRename({ ...rename, display_name: e.target.value })}
              />
            </label>
            <div className="dialog-actions">
              <button className="text-button" onClick={() => setRename(null)}>
                取消
              </button>
              <button
                className="primary"
                disabled={busy || !rename.display_name.trim()}
                onClick={() =>
                  void action(async () => {
                    await schedulesApi.updateTarget(rename.id, {
                      display_name: rename.display_name,
                    });
                    setRename(null);
                    await load();
                  })
                }
              >
                保存
              </button>
            </div>
          </section>
        </div>
      )}
      {pairOpen && (
        <div className="modal-backdrop">
          <section className="modal" role="dialog" aria-modal="true" aria-label="绑定投递位置">
            <button
              className="icon-button modal-close"
              aria-label="关闭绑定"
              onClick={() => setPairOpen(false)}
            >
              <X size={18} />
            </button>
            <h2>绑定投递位置</h2>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            {!pairing ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void action(async () =>
                    setPairing(await schedulesApi.pair(pairType, pairName || undefined)),
                  );
                }}
              >
                <label className="field-label">
                  渠道
                  <Select
                    label="渠道"
                    value={pairType}
                    onChange={(selectedValue) => setPairType(selectedValue)}
                  >
                    <option value="weixin_private">微信</option>
                    <option value="dingtalk_private">钉钉私聊</option>
                    <option value="dingtalk_group">钉钉群聊</option>
                  </Select>
                </label>
                <label className="field-label">
                  名称（可选）
                  <input
                    value={pairName}
                    onChange={(e) => setPairName(e.target.value)}
                    placeholder="例如：我的工作群"
                  />
                </label>
                <button className="primary" disabled={busy}>
                  生成绑定命令
                </button>
              </form>
            ) : (
              <>
                {pairing.target_id ? (
                  <div className="notice">
                    <Check size={17} />
                    绑定完成，可以在任务中选择此位置。
                  </div>
                ) : (
                  <>
                    <p className="muted">
                      将下面的命令发送到要接收结果的 M-Claw 对话或群聊。请先在设置中连接对应渠道。
                    </p>
                    <div className="copy-command">
                      <code>{pairing.command}</code>
                      <button
                        className="icon-button"
                        aria-label="复制绑定命令"
                        onClick={() => void navigator.clipboard.writeText(pairing.command)}
                      >
                        <Copy size={15} />
                      </button>
                    </div>
                    <p className="muted small">
                      {['expired', 'cancelled', 'failed'].includes(pairing.status)
                        ? '此绑定命令已失效，请重新生成。'
                        : `有效期至 ${date(pairing.expires_at)}`}
                    </p>
                    {pairing.error && <p className="inline-error">{pairing.error}</p>}
                    <button
                      className="text-button"
                      onClick={() =>
                        void action(async () => {
                          await schedulesApi.cancelPairing(pairing.code);
                          setPairing(null);
                        })
                      }
                    >
                      取消绑定
                    </button>
                  </>
                )}
              </>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
