import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import {
  Activity,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronDown,
  Clock3,
  FolderOpen,
  FileDiff,
  HelpCircle,
  Menu,
  MessageSquare,
  Monitor,
  Plus,
  Search,
  Settings,
  Store,
  WifiOff,
  Wrench,
  X,
} from 'lucide-react';
import {
  api,
  ApiError,
  filesApi,
  initializeConnection,
  subscribeBrowserLaunch,
  subscribeSession,
} from './api';
import { Composer, WorkspaceDialog } from './components/Composer';
import { Conversation } from './components/Conversation';
import { Authorization } from './components/Authorization';
import { SettingsPage, DeviceStatus } from './components/SettingsPage';
import { ToolsPage } from './components/ToolsPage';
import { SessionSourceIcon } from './components/SessionSourceIcon';
import { DeleteSessionDialog, SessionMenu } from './components/SessionMenu';
import { SkillsPage } from './components/SkillsPage';
import { SkillMarketPage } from './components/SkillMarketPage';
import { SchedulesPage } from './components/SchedulesPage';
import { FilePreviewPanel } from './components/Attachments';
import { ChangesPanel } from './components/ChangesPanel';
import { Setup } from './components/Setup';
import { Providers } from './components/Providers';
import { Welcome } from './components/Welcome';
import { BrandLogo } from './components/BrandLogo';
import { BrandWordmark } from './components/BrandWordmark';
import { StartupScreen } from './components/StartupScreen';
import { TaskCompletionBanner } from './components/TaskCompletionBanner';
import { useTaskNotifications } from './useTaskNotifications';
import { useStartupIntro } from './useStartupIntro';
import {
  groupSessions,
  isPaused,
  isRunning,
  readDrafts,
  readDraftSkills,
  readPendingSubmissions,
  saveDrafts,
  saveDraftSkills,
  savePendingSubmissions,
  STATUS_TEXT,
} from './session-state';
import type { Bootstrap, FileReference, Session, TaskCompletionNotice } from './types';

type Page = 'chat' | 'devices' | 'schedules' | 'skills' | 'market' | 'tools' | 'settings';
const NAV = [
  { id: 'chat', label: '对话', icon: MessageSquare },
  { id: 'devices', label: '设备', icon: Monitor },
  { id: 'schedules', label: '定时任务', icon: Clock3 },
  { id: 'tools', label: '工具', icon: Wrench },
  { id: 'skills', label: '技能', icon: BookOpen },
  { id: 'market', label: '技能市场', icon: Store },
] as const;
const normalize = (session: Session): Session => ({
  ...session,
  messages: session.messages ?? [],
  queue: session.queue ?? [],
});
const completionIds = (session: Session) =>
  session.messages.flatMap((message) =>
    message.task_notification_id ? [message.task_notification_id] : [],
  );

export default function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const startupIntro = useStartupIntro(!!bootstrap);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [cache, setCache] = useState<Record<string, Session>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [welcomeVisit, setWelcomeVisit] = useState(0);
  const [drafts, setDrafts] = useState(readDrafts);
  const [composerFocused, setComposerFocused] = useState(false);
  const [draftSkills, setDraftSkills] = useState(readDraftSkills);
  const [draftDevices, setDraftDevices] = useState(() => readDraftSkills('mclaw.draft-devices.v1'));
  const [draftAttachments, setDraftAttachments] = useState(() =>
    readDraftSkills('mclaw.draft-attachments.v1'),
  );
  const [files, setFiles] = useState<Record<string, FileReference>>({});
  const [previewFile, setPreviewFile] = useState<{
    session: string;
    file: { id: string; name: string };
  } | null>(null);
  const [search, setSearch] = useState('');
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [page, setPage] = useState<Page>('chat');
  const [initialSkillName, setInitialSkillName] = useState<string>();
  useEffect(() => {
    if (page !== 'skills') setInitialSkillName(undefined);
  }, [page]);
  const [error, setError] = useState('');
  const [occupied, setOccupied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadingSession, setLoadingSession] = useState(false);
  const [connected, setConnected] = useState(true);
  const [workspace, setWorkspace] = useState('');
  const [model, setModel] = useState('');
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const [providersOpen, setProvidersOpen] = useState<'providers' | 'refresh' | null>(null);
  const [serviceShutdown, setServiceShutdown] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [rename, setRename] = useState<{ id: string; value: string } | null>(null);
  const [menu, setMenu] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Session | null>(null);
  const deletedSessions = useRef(new Set<string>());
  const [changesOpen, setChangesOpen] = useState(false);
  const [unread, setUnread] = useState<Set<string>>(new Set());
  const [focusedMessage, setFocusedMessage] = useState<{
    session: string;
    id: string;
    request: number;
  }>();
  const focusRequest = useRef(0);
  const historySearch = useRef<HTMLInputElement>(null);
  const usageDetails = useRef<HTMLDetailsElement>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const pendingSubmissions = useRef(readPendingSubmissions());
  const editedDrafts = useRef(new Set<string>());
  const newDraftIdentity = useRef(crypto.randomUUID());
  const movedDrafts = useRef<Record<string, string>>({});
  const session = selected ? (cache[selected] ?? null) : null;
  const notificationSuspended =
    startupIntro.visible ||
    !!deleting ||
    !!providersOpen ||
    workspaceOpen ||
    changesOpen ||
    !!previewFile ||
    sidebarOpen ||
    setupOpen;
  const viewedSessionId =
    page === 'chat' && session && !loadingSession && !occupied && !notificationSuspended
      ? session.id
      : null;
  const notifications = useTaskNotifications(
    !!bootstrap && !bootstrap.setup_required && !serviceShutdown,
    viewedSessionId,
  );
  const acknowledgeNotifications = notifications.acknowledge;
  const observeCompletion = notifications.observeSession;
  useEffect(
    () => setUnread(new Set(notifications.feed.unread_session_ids)),
    [notifications.feed.unread_session_ids],
  );
  const draftKey = selected ?? 'new';
  const draft = drafts[draftKey] ?? '';
  const selectedSkills = draftSkills[draftKey] ?? [];
  const selectedDevices = draftDevices[draftKey] ?? [];
  const selectedAttachments = draftAttachments[draftKey] ?? [];

  const load = useCallback(async () => {
    setError('');
    try {
      await initializeConnection();
      const next = await api.bootstrap();
      setBootstrap(next);
      setModel((current) => current || next.default_model || next.models?.[0]?.id || '');
      const history = await api.sessions();
      setSessions(history.filter((item) => !deletedSessions.current.has(item.id)).map(normalize));
      setHasMore(history.length === 50);
      setConnected(true);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
      setConnected(false);
    }
  }, []);
  useEffect(() => {
    void load();
    return subscribeBrowserLaunch(() => window.location.reload());
  }, [load]);
  useEffect(() => {
    saveDrafts(drafts);
  }, [drafts]);
  useEffect(() => {
    saveDraftSkills(draftSkills);
  }, [draftSkills]);
  useEffect(() => saveDraftSkills(draftDevices, 'mclaw.draft-devices.v1'), [draftDevices]);
  useEffect(
    () => saveDraftSkills(draftAttachments, 'mclaw.draft-attachments.v1'),
    [draftAttachments],
  );
  useEffect(() => {
    if (!selected || occupied || loadingSession) return;
    let active = true;
    for (const id of selectedAttachments) {
      if (files[id]) continue;
      void filesApi
        .metadata(selected, id)
        .then((file) => {
          if (active) setFiles((current) => ({ ...current, [file.id]: file }));
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    }
    return () => {
      active = false;
    };
  }, [selected, draftAttachments, occupied, loadingSession]);
  useEffect(() => {
    if (!selected || !editedDrafts.current.has(selected) || occupied || loadingSession || busy)
      return;
    const timeout = setTimeout(() => {
      void api
        .updateSession(selected, {
          draft,
          draft_skills: draftSkills[selected] ?? [],
          draft_attachments: draftAttachments[selected] ?? [],
          draft_devices: draftDevices[selected] ?? [],
        })
        .catch(() => {});
    }, 450);
    return () => clearTimeout(timeout);
  }, [
    selected,
    draft,
    draftSkills,
    draftAttachments,
    draftDevices,
    occupied,
    loadingSession,
    busy,
  ]);
  useEffect(() => {
    if (!bootstrap) return;
    let disposed = false;
    const timeout = setTimeout(() => {
      void api
        .sessions({ q: search })
        .then((items) => {
          if (!disposed) {
            setSessions(
              items.filter((item) => !deletedSessions.current.has(item.id)).map(normalize),
            );
            setHasMore(items.length === 50);
          }
        })
        .catch((e) => {
          if (!disposed) setError(e.message);
        });
    }, 250);
    return () => {
      disposed = true;
      clearTimeout(timeout);
    };
  }, [search, bootstrap]);
  useEffect(() => {
    if (!bootstrap || bootstrap.setup_required || serviceShutdown) return;
    const interval = setInterval(
      () =>
        api
          .sessions({ q: search })
          .then((next) => {
            const complete = next.length < 50;
            next = next.filter((item) => !deletedSessions.current.has(item.id));
            setConnected(true);
            setSessions((previous) => {
              const old = new Map(previous.map((item) => [item.id, item.status]));
              const refreshed = new Map(next.map((item) => [item.id, normalize(item)]));
              // A short page is the complete result, including deletions made
              // from another window while this page was disconnected.
              if (complete) return next.map(normalize);
              return [
                ...next.filter((item) => !old.has(item.id)).map(normalize),
                ...previous.map((item) => refreshed.get(item.id) ?? item),
              ];
            });
          })
          .catch(() => setConnected(false)),
      5000,
    );
    return () => clearInterval(interval);
  }, [bootstrap, search, serviceShutdown]);
  const merge = useCallback(
    (next: Session) => {
      if (deletedSessions.current.has(next.id)) return;
      const safe = normalize(next);
      // Observe the completion before a later navigation can change the page,
      // including when the global notification poll has not arrived yet.
      observeCompletion(safe);
      const latest = (previous?: Session) =>
        previous?.seq !== undefined && safe.seq !== undefined && previous.seq > safe.seq
          ? previous
          : safe;
      setCache((current) => ({ ...current, [safe.id]: latest(current[safe.id]) }));
      setSessions((current) =>
        current.some((item) => item.id === safe.id)
          ? current.map((item) => (item.id === safe.id ? latest(item) : item))
          : [safe, ...current],
      );
    },
    [observeCompletion],
  );
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoadingSession(true);
    setOccupied(false);
    setError('');
    api
      .session(selected)
      .then((next) => {
        if (!cancelled && !deletedSessions.current.has(next.id)) {
          merge(next);
          setDrafts((current) =>
            editedDrafts.current.has(selected) || current[selected] !== undefined
              ? current
              : { ...current, [selected]: next.draft ?? '' },
          );
          setDraftSkills((current) =>
            editedDrafts.current.has(selected) || current[selected] !== undefined
              ? current
              : { ...current, [selected]: next.draft_skills ?? [] },
          );
          setDraftAttachments((current) =>
            editedDrafts.current.has(selected) || current[selected] !== undefined
              ? current
              : { ...current, [selected]: next.draft_attachments ?? [] },
          );
          setDraftDevices((current) =>
            editedDrafts.current.has(selected)
              ? current
              : { ...current, [selected]: next.draft_devices ?? [] },
          );
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e.message);
          setOccupied(
            e instanceof ApiError &&
              ['occupied', 'session_occupied', 'ownership_conflict'].includes(e.code.toLowerCase()),
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingSession(false);
      });
    const dispose = subscribeSession(
      selected,
      (event) => {
        if (event.session) merge(event.session);
        if (event.type === 'occupied') {
          setOccupied(true);
          setError('该会话正由其他窗口或终端控制。');
        }
      },
      setConnected,
    );
    return () => {
      cancelled = true;
      dispose();
    };
  }, [selected, merge]);
  useEffect(() => {
    if (session) observeCompletion(session);
  }, [session, viewedSessionId, notifications.foreground, observeCompletion]);

  const changeDraft = (value: string) => {
    editedDrafts.current.add(draftKey);
    setDrafts((current) => ({ ...current, [draftKey]: value }));
  };
  const changeSkills = (names: string[]) => {
    editedDrafts.current.add(draftKey);
    setDraftSkills((current) => ({ ...current, [draftKey]: names }));
  };
  const useSkill = (name: string) => {
    changeSkills(selectedSkills.includes(name) ? selectedSkills : [...selectedSkills, name]);
    setPage('chat');
  };
  const ensureSession = async () => {
    if (session) return session.id;
    setBusy(true);
    try {
      const next = normalize(
        await api.createSession({ workspace: workspace || undefined, model: model || undefined }),
      );
      merge(next);
      editedDrafts.current.add(next.id);
      movedDrafts.current[`new:${newDraftIdentity.current}`] = next.id;
      newDraftIdentity.current = crypto.randomUUID();
      setDrafts((current) => ({ ...current, [next.id]: current.new ?? '', new: '' }));
      setDraftSkills((current) => ({ ...current, [next.id]: current.new ?? [], new: [] }));
      setDraftDevices((current) => ({ ...current, [next.id]: current.new ?? [], new: [] }));
      setDraftAttachments((current) => ({ ...current, [next.id]: current.new ?? [], new: [] }));
      setSelected(next.id);
      return next.id;
    } finally {
      setBusy(false);
    }
  };
  const loadMore = async () => {
    setLoadingMore(true);
    try {
      const next = await api.sessions({ q: search, offset: sessions.length });
      setSessions((current) => [
        ...current,
        ...next
          .filter(
            (item) =>
              !deletedSessions.current.has(item.id) &&
              !current.some((previous) => previous.id === item.id),
          )
          .map(normalize),
      ]);
      setHasMore(next.length === 50);
    } catch (e) {
      setError(e instanceof Error ? e.message : '历史加载失败');
    } finally {
      setLoadingMore(false);
    }
  };
  const newSession = () => {
    setMenu(null);
    setWelcomeVisit((visit) => visit + 1);
    setSelected(null);
    setPage('chat');
    setError('');
    setOccupied(false);
    setWorkspace('');
    setModel(bootstrap?.default_model ?? bootstrap?.models?.[0]?.id ?? '');
    setSidebarOpen(false);
  };
  const openSession = (id: string) => {
    setMenu(null);
    setSelected(id);
    setPage('chat');
    setSidebarOpen(false);
  };
  const viewCompletion = async (notice: TaskCompletionNotice) => {
    try {
      const next = await api.session(notice.session_id);
      merge(next);
      openSession(notice.session_id);
      setFocusedMessage({
        session: notice.session_id,
        id: notice.response_id,
        request: ++focusRequest.current,
      });
      await acknowledgeNotifications([...new Set([notice.id, ...completionIds(next)])], 'read');
    } catch (e) {
      setError(e instanceof Error ? e.message : '查看结果失败，请重试');
      throw e;
    }
  };
  const dismissCompletion = async (notice: TaskCompletionNotice) => {
    try {
      await acknowledgeNotifications(
        [notice.id],
        page === 'chat' && selected === notice.session_id ? 'read' : 'dismiss',
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : '关闭通知失败，请重试');
      throw e;
    }
  };
  const runAction = async (action: () => Promise<Session>) => {
    setBusy(true);
    setError('');
    try {
      const next = await action();
      merge(next);
      return next;
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败');
      throw e;
    } finally {
      setBusy(false);
    }
  };
  const send = async (mode: 'queue' | 'steer') => {
    if (busy || occupied || (!draft.trim() && !isPaused(session))) return;
    const originalKey = draftKey;
    const content = draft;
    const skills = [...selectedSkills];
    const devices = [...selectedDevices];
    const attachments = [...selectedAttachments];
    let target = session;
    try {
      setBusy(true);
      setError('');
      if (!target) {
        target = normalize(
          await api.createSession({ workspace: workspace || undefined, model: model || undefined }),
        );
        merge(target);
        movedDrafts.current[`new:${newDraftIdentity.current}`] = target.id;
        newDraftIdentity.current = crypto.randomUUID();
        setSelected(target.id);
        setDraftDevices((current) => ({
          ...current,
          [target!.id]: current[originalKey] ?? devices,
          [originalKey]: [],
        }));
        setDrafts((current) => ({
          ...current,
          [target!.id]: current[originalKey] ?? content,
          [originalKey]: '',
        }));
        setDraftSkills((current) => ({
          ...current,
          [target!.id]: current[originalKey] ?? skills,
          [originalKey]: [],
        }));
        setDraftAttachments((current) => ({
          ...current,
          [target!.id]: current[originalKey] ?? attachments,
          [originalKey]: [],
        }));
      }
      const key = JSON.stringify([target.id, mode, content, skills, attachments, devices]);
      if (!pendingSubmissions.current[key]) {
        pendingSubmissions.current[key] = crypto.randomUUID();
        savePendingSubmissions(pendingSubmissions.current);
      }
      const resume = isPaused(target);
      const supplement = resume && !!target.active_task ? content.trim() || undefined : undefined;
      const next = resume
        ? await api.resume(
            target.id,
            supplement,
            pendingSubmissions.current[key],
            supplement ? skills : undefined,
            supplement ? attachments : undefined,
          )
        : await api.submit(
            target.id,
            content,
            mode,
            pendingSubmissions.current[key],
            skills,
            attachments,
            devices,
          );
      merge(next);
      delete pendingSubmissions.current[key];
      savePendingSubmissions(pendingSubmissions.current);
      if (!resume || supplement) {
        editedDrafts.current.add(target.id);
        setDrafts((current) => ({
          ...current,
          [target!.id]: current[target!.id] === content ? '' : (current[target!.id] ?? ''),
        }));
        setDraftSkills((current) => ({
          ...current,
          [target!.id]:
            JSON.stringify(current[target!.id] ?? []) === JSON.stringify(skills)
              ? []
              : (current[target!.id] ?? []),
        }));
        setDraftAttachments((current) => ({
          ...current,
          [target!.id]:
            JSON.stringify(current[target!.id] ?? []) === JSON.stringify(attachments)
              ? []
              : (current[target!.id] ?? []),
        }));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '发送失败，草稿已保留');
    } finally {
      setBusy(false);
    }
  };
  const exportSession = async (item: Session) => {
    try {
      const full = await api.exportSession(item.id);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(full, null, 2)], { type: 'application/json' }),
      );
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `mclaw-${item.id}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : '导出失败');
    }
  };
  const deleteSession = async (item: Session) => {
    await api.deleteSession(item.id);
    deletedSessions.current.add(item.id);
    const withoutSession = <T,>(values: Record<string, T>) =>
      Object.fromEntries(Object.entries(values).filter(([id]) => id !== item.id));
    setSessions((current) => current.filter((value) => value.id !== item.id));
    setCache(withoutSession);
    setDrafts(withoutSession);
    setDraftSkills(withoutSession);
    setDraftAttachments(withoutSession);
    editedDrafts.current.delete(item.id);
    pendingSubmissions.current = Object.fromEntries(
      Object.entries(pendingSubmissions.current).filter(([key]) => {
        try {
          return JSON.parse(key)[0] !== item.id;
        } catch {
          return true;
        }
      }),
    );
    savePendingSubmissions(pendingSubmissions.current);
    setUnread((current) => {
      const next = new Set(current);
      next.delete(item.id);
      return next;
    });
    if (previewFile?.session === item.id) setPreviewFile(null);
    if (selectedRef.current === item.id) {
      setChangesOpen(false);
      setWorkspaceOpen(false);
      newSession();
    }
    setDeleting(null);
  };

  const withStartup = (content: ReactNode) => (
    <>
      {startupIntro.visible && (
        <StartupScreen
          leaving={startupIntro.leaving}
          ready={!!bootstrap}
          error={error}
          onRetry={load}
        />
      )}
      <div
        className="startup-content"
        inert={startupIntro.visible}
        aria-hidden={startupIntro.visible}
      >
        {content}
      </div>
    </>
  );
  if (!bootstrap) return withStartup(null);
  if (bootstrap.setup_required || setupOpen)
    return withStartup(
      <Setup
        active={!startupIntro.visible}
        onComplete={async () => {
          setSetupOpen(false);
          await load();
        }}
        onClose={bootstrap.setup_required ? undefined : () => setSetupOpen(false)}
      />,
    );
  const sessionError = typeof session?.error === 'string' ? session.error : session?.error?.message;
  return withStartup(
    <div className="app-shell">
      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="关闭导航"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <a
          className="brand"
          aria-label="M-Claw"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            newSession();
          }}
        >
          <BrandLogo />
          <BrandWordmark />
        </a>
        <button className="new-chat" onClick={newSession}>
          <Plus size={18} />
          新建会话
        </button>
        <nav className="main-nav">
          {NAV.map((item) => (
            <button
              key={item.id}
              className={page === item.id ? 'active' : ''}
              onClick={() => {
                setMenu(null);
                setPage(item.id);
                setSidebarOpen(false);
              }}
            >
              <item.icon size={17} />
              {item.label}
            </button>
          ))}
        </nav>
        <div className="history-heading">
          <span>会话</span>
          <span>{sessions.length}</span>
        </div>
        <label className="session-search">
          <Search size={14} />
          <input
            ref={historySearch}
            placeholder="搜索会话"
            aria-label="搜索会话"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          {search && (
            <button className="icon-button" aria-label="清除搜索" onClick={() => setSearch('')}>
              <X size={12} />
            </button>
          )}
        </label>
        <div className="session-list">
          {groupSessions(sessions, '').map((group) => (
            <section key={group.title}>
              <h2 title={group.title}>
                {group.title === '最近会话' ? (
                  group.title
                ) : (
                  <>
                    <FolderOpen size={12} />
                    {group.title.split(/[\\/]/).filter(Boolean).pop()}
                  </>
                )}
              </h2>
              {group.sessions.map((item) => (
                <div
                  className={`session-row ${selected === item.id && page === 'chat' ? 'selected' : ''}`}
                  key={item.id}
                >
                  {rename?.id === item.id ? (
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        void runAction(() =>
                          api.updateSession(item.id, { title: rename.value.trim() }),
                        )
                          .then(() => setRename(null))
                          .catch(() => {});
                      }}
                    >
                      <input
                        autoFocus
                        aria-label="会话名称"
                        value={rename.value}
                        onChange={(e) => setRename({ id: item.id, value: e.target.value })}
                      />
                      <button
                        className="icon-button"
                        aria-label="保存名称"
                        disabled={!rename.value.trim() || busy}
                      >
                        <Check size={14} />
                      </button>
                      <button
                        type="button"
                        className="icon-button"
                        aria-label="取消重命名"
                        onClick={() => setRename(null)}
                      >
                        <X size={14} />
                      </button>
                    </form>
                  ) : (
                    <>
                      <button
                        className="session-link"
                        onClick={() => openSession(item.id)}
                        title={item.title}
                      >
                        <span
                          className={`session-state ${isRunning(item) ? 'running-ring' : isPaused(item) ? 'paused-marker' : unread.has(item.id) ? 'unread-dot' : ''}`}
                          aria-label={STATUS_TEXT[item.status]}
                        />{' '}
                        <SessionSourceIcon source={item.source} size={16} />
                        <span>{item.title || '新会话'}</span>
                      </button>
                      <SessionMenu
                        session={item}
                        open={menu === item.id}
                        onOpenChange={(open) => setMenu(open ? item.id : null)}
                        onRename={() => setRename({ id: item.id, value: item.title })}
                        onExport={() => void exportSession(item)}
                        onDelete={() => setDeleting(item)}
                      />
                    </>
                  )}
                </div>
              ))}
            </section>
          ))}
          {!sessions.length && (
            <p className="empty-history">
              {search ? (
                '没有找到匹配的会话'
              ) : (
                <>
                  从一条消息开始，
                  <br />
                  你的会话会保存在这里。
                </>
              )}
            </p>
          )}
          {hasMore && (
            <button
              className="load-more text-button"
              disabled={loadingMore}
              onClick={() => void loadMore()}
            >
              {loadingMore ? '正在加载…' : '加载更多历史'}
            </button>
          )}
        </div>
        <div className="sidebar-bottom">
          <button
            onClick={() => {
              setPage('settings');
              setSidebarOpen(false);
            }}
            className={page === 'settings' ? 'active' : ''}
          >
            <Settings size={17} />
            设置
          </button>
          <a
            href="https://gitcode.com/m-robots/mclaw/blob/main/docs/manual/README.md"
            target="_blank"
            rel="noreferrer"
          >
            <HelpCircle size={17} />
            帮助
          </a>
          <div className="host-info">
            <span className={`status-dot ${connected ? '' : 'offline'}`} />
            <span>
              {bootstrap.device?.name ?? '本机服务'}
              <small>
                {bootstrap.device?.platform ?? 'M-Claw'} · {connected ? '已连接' : '连接中断'}
              </small>
            </span>
          </div>
        </div>
      </aside>
      <main
        className={`main-panel ${page === 'chat' && !session?.messages.length ? 'welcome-view' : ''}`}
      >
        <header className={`topbar ${page !== 'chat' ? 'management-topbar' : ''}`}>
          <button
            className="icon-button mobile-menu"
            aria-label="打开导航"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu size={20} />
          </button>
          <div className="title-block">
            <h1>
              {page === 'chat' && <SessionSourceIcon source={session?.source} />}
              {page === 'chat'
                ? session?.title || '新会话'
                : page === 'settings'
                  ? '设置'
                  : NAV.find((item) => item.id === page)?.label}
            </h1>
            <span role={page === 'chat' && session?.status === 'running' ? 'status' : undefined}>
              <span className={`status-dot ${!connected ? 'offline' : ''}`} />
              {page === 'chat'
                ? session?.status === 'running'
                  ? session.status_text?.replace('正在处理你的任务…', '处理中…') || '处理中…'
                  : session
                    ? STATUS_TEXT[session.status]
                    : '准备就绪'
                : '当前设备'}
            </span>
          </div>
          <div className="topbar-right">
            {page === 'chat' && session && (
              <button className="text-button" onClick={() => setChangesOpen(true)}>
                <FileDiff size={15} />
                变更
              </button>
            )}
            <Monitor size={15} />
            <span>{bootstrap.device?.name ?? '本机'}</span>
            {session?.usage && (
              <details className="usage" ref={usageDetails}>
                <summary>
                  {(
                    session.usage.total_tokens ??
                    (session.usage.input_tokens ?? 0) + (session.usage.output_tokens ?? 0)
                  ).toLocaleString()}{' '}
                  tokens <ChevronDown size={12} />
                </summary>
                <div>
                  输入 {session.usage.input_tokens ?? 0}
                  <br />
                  输出 {session.usage.output_tokens ?? 0}
                  <br />
                  缓存 {session.usage.cache_tokens ?? 0}
                </div>
              </details>
            )}
          </div>
        </header>
        {!connected && !serviceShutdown && (
          <div className="connection-banner" role="status">
            <WifiOff size={16} />
            连接已中断，正在重新连接。服务运行期间，已提交任务会继续执行。
          </div>
        )}
        {(error || sessionError) && (
          <div className="error-banner main-error" role="alert">
            <span>{error || sessionError}</span>
            <button className="text-button" onClick={() => setProvidersOpen('providers')}>
              打开设置
            </button>
            <button className="icon-button" aria-label="关闭提示" onClick={() => setError('')}>
              <X size={15} />
            </button>
          </div>
        )}
        {page === 'chat' ? (
          <>
            {loadingSession && !session ? (
              <div className="content-loading">正在加载会话与运行状态…</div>
            ) : !session?.messages.length ? (
              <Welcome
                key={`${selected ?? 'new'}:${welcomeVisit}`}
                active={!startupIntro.visible}
                quiet={composerFocused || draft.length > 0}
                onPrompt={(prompt) => {
                  changeDraft(prompt);
                  document.getElementById('task-draft')?.focus();
                }}
              />
            ) : (
              <Conversation
                session={session}
                focusedMessage={focusedMessage?.session === session.id ? focusedMessage : undefined}
                onFilePreview={(file) => setPreviewFile({ session: session.id, file })}
                onOpenSkill={(name) => {
                  setInitialSkillName(name);
                  setPage('skills');
                }}
              />
            )}
            {occupied && (
              <div className="notice">该会话由其他窗口或终端控制。请在原入口继续操作。</div>
            )}
            {session?.pending_request && (
              <div className="authorization-wrap">
                <Authorization
                  key={session.pending_request.id}
                  request={session.pending_request}
                  busy={busy || occupied || !connected}
                  onReply={async (value) => {
                    await runAction(() =>
                      api.reply(session.id, session.pending_request!.id, value),
                    );
                  }}
                />
              </div>
            )}
            <Composer
              collaboration={bootstrap.device.platform === 'kaihong'}
              selectedDevices={selectedDevices}
              onDevicesChange={(ids) => {
                editedDrafts.current.add(draftKey);
                setDraftDevices((current) => ({ ...current, [draftKey]: ids }));
              }}
              commands={[
                { name: 'clear', description: '新建会话', run: newSession },
                {
                  name: 'history',
                  description: '查找历史会话',
                  run: () => {
                    setSidebarOpen(true);
                    historySearch.current?.focus();
                  },
                },
                ...(isPaused(session) && session
                  ? [
                      {
                        name: 'continue',
                        description: '继续原任务与队列',
                        run: () => {
                          void runAction(() => api.resume(session.id)).catch(() => {});
                        },
                      },
                    ]
                  : []),
              ]}
              ensureSession={ensureSession}
              attachments={selectedAttachments.map(
                (id) => files[id] ?? { id, name: '正在读取附件…' },
              )}
              onAttachmentAdded={(id, file) => {
                editedDrafts.current.add(id);
                setFiles((current) => ({ ...current, [file.id]: file }));
                setDraftAttachments((current) => ({
                  ...current,
                  [id]: [...new Set([...(current[id] ?? []), file.id])],
                }));
              }}
              onAttachmentRemove={(id) => {
                editedDrafts.current.add(draftKey);
                setDraftAttachments((current) => ({
                  ...current,
                  [draftKey]: (current[draftKey] ?? []).filter((item) => item !== id),
                }));
              }}
              onFilePreview={(file) => {
                if (selected) setPreviewFile({ session: selected, file });
              }}
              draftKey={selected ?? `new:${newDraftIdentity.current}`}
              onTranscript={(recordingKey, text) => {
                if (!text.trim()) return;
                const key =
                  movedDrafts.current[recordingKey] ??
                  (recordingKey.startsWith('new:') ? 'new' : recordingKey);
                editedDrafts.current.add(key);
                setDrafts((current) => ({
                  ...current,
                  [key]: [current[key], text].filter(Boolean).join('\n'),
                }));
              }}
              session={session}
              draft={draft}
              onFocusChange={setComposerFocused}
              onDraft={changeDraft}
              onSend={(mode) => void send(mode)}
              onStop={() => {
                if (session) void runAction(() => api.stop(session.id)).catch(() => {});
              }}
              onModel={(value) => {
                if (session)
                  void runAction(() => api.updateSession(session.id, { model: value })).catch(
                    () => {},
                  );
                else setModel(value);
              }}
              onManageProviders={() => setProvidersOpen('providers')}
              onQueue={async (items) => {
                if (session)
                  await runAction(() =>
                    api.queue(
                      session.id,
                      items.map((item) => ({
                        id: item.id,
                        content: item.content,
                        ...(item.skill_names ? { skills: item.skill_names } : {}),
                        ...(item.attachment_ids ? { attachments: item.attachment_ids } : {}),
                      })),
                    ),
                  );
              }}
              selectedSkills={selectedSkills}
              onSkillsChange={changeSkills}
              onWorkspace={() => setWorkspaceOpen(true)}
              workspace={session?.workspace ?? workspace}
              models={bootstrap.models ?? []}
              model={session?.model ?? model}
              busy={busy}
              disabled={occupied || loadingSession || !connected || serviceShutdown}
            />
          </>
        ) : page === 'tools' ? (
          <ToolsPage />
        ) : page === 'skills' ? (
          <SkillsPage onUse={useSkill} initialSkillName={initialSkillName} />
        ) : page === 'market' ? (
          <SkillMarketPage
            onManage={(name) => {
              setInitialSkillName(name);
              setPage('skills');
            }}
          />
        ) : page === 'schedules' ? (
          <SchedulesPage onSession={openSession} models={bootstrap.models} />
        ) : page === 'settings' ? (
          <SettingsPage
            bootstrap={bootstrap}
            onSetup={() => setSetupOpen(true)}
            onProviders={() => setProvidersOpen('providers')}
            onShutdown={() => setServiceShutdown(true)}
          />
        ) : (
          <DeviceStatus bootstrap={bootstrap} />
        )}
      </main>
      <TaskCompletionBanner
        notice={notifications.feed.notifications.find(
          (notice) => !deletedSessions.current.has(notice.session_id),
        )}
        count={notifications.feed.pending_count}
        suspended={notificationSuspended}
        onView={viewCompletion}
        onDismiss={dismissCompletion}
      />
      {deleting && (
        <DeleteSessionDialog
          session={deleting}
          onClose={() => setDeleting(null)}
          onDelete={() => deleteSession(deleting)}
        />
      )}
      {providersOpen && (
        <Providers
          onClose={() => setProvidersOpen(null)}
          refreshOnOpen={providersOpen === 'refresh'}
          onUpdated={async () => {
            const next = await api.bootstrap();
            setBootstrap(next);
            if (!selected && model === bootstrap.default_model) setModel(next.default_model ?? '');
          }}
        />
      )}
      {previewFile && (
        <FilePreviewPanel
          session={previewFile.session}
          file={previewFile.file}
          onClose={() => setPreviewFile(null)}
        />
      )}
      {changesOpen && session && (
        <ChangesPanel
          key={session.id}
          session={session}
          onApplied={merge}
          onClose={() => setChangesOpen(false)}
        />
      )}
      {workspaceOpen && (
        <WorkspaceDialog
          value={session?.workspace ?? workspace}
          onSave={(value) => {
            if (session)
              void runAction(() => api.updateSession(session.id, { workspace: value }))
                .then(() => setWorkspaceOpen(false))
                .catch(() => {});
            else {
              setWorkspace(value);
              setWorkspaceOpen(false);
            }
          }}
          onClose={() => setWorkspaceOpen(false)}
        />
      )}
    </div>,
  );
}
