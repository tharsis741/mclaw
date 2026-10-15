import {
  Bug,
  Clock3,
  Code2,
  FileText,
  FolderOpen,
  Languages,
  Lightbulb,
  ListChecks,
  PenLine,
  Presentation,
  Search,
  Table2,
  type LucideIcon,
} from 'lucide-react';
import { LAYOUT_NAMES, type CompanionLayout } from './components/companion-layouts';

type WelcomeAction = { id: string; title: string; text: string; icon: LucideIcon };

export const WELCOME_ACTIONS: readonly WelcomeAction[] = [
  {
    id: 'files',
    title: '整理文件',
    text: '帮我看看这个目录里的文件，怎么整理比较合适？',
    icon: FolderOpen,
  },
  {
    id: 'research',
    title: '查找资料',
    text: '我想查一些资料，帮我对照不同来源整理一下，并附上链接。',
    icon: Search,
  },
  {
    id: 'document',
    title: '总结文档',
    text: '帮我读一份文档，提炼主要内容和需要留意的地方。',
    icon: FileText,
  },
  {
    id: 'table',
    title: '分析表格',
    text: '帮我分析一份表格，看看有哪些趋势和值得关注的数据。',
    icon: Table2,
  },
  {
    id: 'writing',
    title: '起草文案',
    text: '我想写一段文案，先和我聊聊用途和想表达的内容。',
    icon: PenLine,
  },
  {
    id: 'translate',
    title: '翻译内容',
    text: '帮我翻译一段内容，保留原意、语气和格式。',
    icon: Languages,
  },
  {
    id: 'slides',
    title: '制作演示',
    text: '帮我准备一份演示文稿，我们先把主题和结构定下来。',
    icon: Presentation,
  },
  { id: 'idea', title: '梳理想法', text: '我有一个想法，想和你一起理一理。', icon: Lightbulb },
  {
    id: 'plan',
    title: '制定计划',
    text: '帮我把一个目标拆成具体步骤，安排好先后顺序和时间。',
    icon: ListChecks,
  },
  {
    id: 'reminder',
    title: '设个提醒',
    text: '我想设个提醒，和我确认一下内容、时间，以及要不要重复。',
    icon: Clock3,
  },
  {
    id: 'project',
    title: '读懂项目',
    text: '带我看看这个项目，先从主要结构和运行方式讲起。',
    icon: Code2,
  },
  {
    id: 'debug',
    title: '排查报错',
    text: '我遇到了一个报错，帮我结合代码或日志看看原因。',
    icon: Bug,
  },
];

type LastWelcome = { actionIds: string[]; layout?: CompanionLayout };
const STORAGE_KEY = 'mclaw.welcome.v1';

export function readLastWelcome(): LastWelcome | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null');
    if (!value || typeof value !== 'object') return null;
    const last = value as { actionIds?: unknown; layout?: unknown; expression?: unknown };
    const oldLayouts: Record<string, CompanionLayout> = { bridge: 'portrait', balance: 'cascade' };
    const layout =
      typeof last.layout === 'string'
        ? LAYOUT_NAMES.includes(last.layout as CompanionLayout)
          ? (last.layout as CompanionLayout)
          : Object.prototype.hasOwnProperty.call(oldLayouts, last.layout)
            ? oldLayouts[last.layout]
            : undefined
        : undefined;
    return Array.isArray(last.actionIds) &&
      last.actionIds.every((id) => typeof id === 'string') &&
      (layout !== undefined || typeof last.expression === 'string')
      ? {
          actionIds: last.actionIds,
          layout,
        }
      : null;
  } catch {
    return null;
  }
}

export function chooseWelcome(previous: LastWelcome | null, random = Math.random) {
  const pool = [...WELCOME_ACTIONS];
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  // A new visit changes the assortment too, even if the shuffle draws the same six.
  if (
    previous?.actionIds.length === 6 &&
    pool.slice(0, 6).every(({ id }) => previous.actionIds.includes(id))
  ) {
    [pool[5], pool[6]] = [pool[6], pool[5]];
  }
  const layouts = LAYOUT_NAMES.filter((id) => id !== previous?.layout);
  return {
    actions: pool.slice(0, 6),
    layout: layouts[Math.floor(random() * layouts.length)],
  };
}

export function rememberWelcome(selection: ReturnType<typeof chooseWelcome>) {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        actionIds: selection.actions.map(({ id }) => id),
        layout: selection.layout,
      }),
    );
  } catch {
    // Welcome preferences are optional when browser storage is unavailable.
  }
}
