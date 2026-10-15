import { useEffect, useId, useRef, useState } from 'react';
import type { ChangeEvent, CSSProperties, KeyboardEvent, RefObject } from 'react';
import { createPortal } from 'react-dom';
import {
  BookOpen,
  ChartNoAxesCombined,
  Check,
  CircleHelp,
  Clock3,
  CornerDownLeft,
  Download,
  FileDiff,
  History,
  ListOrdered,
  Monitor,
  Pencil,
  Plug,
  Play,
  RefreshCw,
  Settings,
  SlidersHorizontal,
  Square,
  SquarePen,
  Terminal,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { skillsApi } from '../api';
import type { ModelOption, SkillSummary } from '../types';

export interface SlashCommand {
  name: string;
  description: string;
  run: () => void;
}

type Choice = SlashCommand & {
  id: string;
  kind: 'command' | 'skill' | 'model';
  selected?: boolean;
};

const COMMAND_ICONS: Record<string, LucideIcon> = {
  help: CircleHelp,
  clear: SquarePen,
  history: History,
  skills: BookOpen,
  schedule: Clock3,
  devices: Monitor,
  provider: Plug,
  settings: Settings,
  title: Pencil,
  save: Download,
  rollback: FileDiff,
  usage: ChartNoAxesCombined,
  continue: Play,
  queue: ListOrdered,
  stop: Square,
  model: SlidersHorizontal,
  'model-update': RefreshCw,
};

function ChoiceIcon({ choice }: { choice: Choice }) {
  const Icon =
    choice.kind === 'skill'
      ? BookOpen
      : choice.kind === 'model'
        ? SlidersHorizontal
        : (COMMAND_ICONS[choice.name] ?? Terminal);
  return <Icon size={16} aria-hidden="true" />;
}

// Only a leading, single token opens completion. Paths and normal prose keep Enter-to-send.
function parseQuery(draft: string) {
  const model = /^\/model[ \t]+([^\r\n]*)$/i.exec(draft);
  if (model) return { kind: 'model' as const, text: model[1].toLowerCase() };
  const command = /^\/([^\s/\\]*)$/.exec(draft);
  return command ? { kind: 'command' as const, text: command[1].toLowerCase() } : null;
}

export function useSlashCommands({
  input,
  draft,
  onDraft,
  commands,
  selectedSkills,
  onSkillsChange,
  models,
  model,
  onModel,
  disabled,
  contextKey,
}: {
  input: RefObject<HTMLTextAreaElement | null>;
  draft: string;
  onDraft: (value: string) => void;
  commands: SlashCommand[];
  selectedSkills: string[];
  onSkillsChange: (names: string[]) => void;
  models: ModelOption[];
  model: string;
  onModel: (id: string) => void;
  disabled: boolean;
  contextKey?: string;
}) {
  const id = useId();
  const popup = useRef<HTMLDivElement>(null);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [composing, setComposing] = useState(false);
  const [active, setActive] = useState(0);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [position, setPosition] = useState<CSSProperties>({});
  const query = parseQuery(draft);
  const open = !!query && focused && !dismissed && !composing && !disabled;
  const loadSkills = open && query?.kind === 'command';

  useEffect(() => {
    setDismissed(true);
    setActive(0);
  }, [contextKey]);
  useEffect(() => setActive(0), [draft]);
  useEffect(() => {
    if (!loadSkills) return;
    let current = true;
    setSkills([]);
    setLoading(true);
    setError('');
    void skillsApi.list().then(
      (result) => {
        if (current) {
          setSkills(result.skills);
          setLoading(false);
        }
      },
      () => {
        if (current) {
          setSkills([]);
          setError('技能暂时无法加载，可以重新输入 / 重试。');
          setLoading(false);
        }
      },
    );
    return () => {
      current = false;
    };
  }, [loadSkills]);

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const bounds = input.current?.closest('form')?.getBoundingClientRect();
      if (!bounds) return;
      const width = Math.min(480, bounds.width, window.innerWidth - 24);
      const above = bounds.top - 20;
      const below = window.innerHeight - bounds.bottom - 20;
      setPosition({
        left: Math.max(12, Math.min(bounds.left, window.innerWidth - width - 12)),
        width,
        ...(above >= below
          ? { bottom: window.innerHeight - bounds.top + 8, maxHeight: Math.max(80, above) }
          : { top: bounds.bottom + 8, maxHeight: Math.max(80, below) }),
      });
    };
    const outside = (event: PointerEvent) => {
      if (!popup.current?.contains(event.target as Node) && event.target !== input.current)
        setDismissed(true);
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    document.addEventListener('pointerdown', outside, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      document.removeEventListener('pointerdown', outside, true);
    };
  }, [open, input]);

  const choices: Choice[] =
    query?.kind === 'model'
      ? models.map((option) => ({
          id: `model:${option.id}`,
          kind: 'model',
          name: option.id,
          description: option.label,
          selected: model === option.id,
          run: () => onModel(option.id),
        }))
      : [
          ...commands.map((command): Choice => ({
            ...command,
            id: `command:${command.name}`,
            kind: 'command',
          })),
          {
            id: 'command:model',
            kind: 'command',
            name: 'model',
            description: '切换会话模型',
            run: () => onDraft('/model '),
          },
          ...skills
            .filter((skill) => skill.enabled)
            .map((skill): Choice => ({
              id: `skill:${skill.name}`,
              kind: 'skill',
              name: skill.name,
              description: skill.description,
              selected: selectedSkills.includes(skill.name),
              run: () => onSkillsChange([...new Set([...selectedSkills, skill.name])]),
            })),
        ];
  const matches = choices.filter((choice) =>
    `${choice.name} ${choice.description}`.toLowerCase().includes(query?.text ?? ''),
  );
  // Exact names come first, so /weather selects that skill even if another description mentions it.
  const rank = (choice: Choice) =>
    choice.name.toLowerCase() === query?.text
      ? 0
      : choice.name.toLowerCase().startsWith(query?.text ?? '')
        ? 1
        : 2;
  matches.sort((a, b) => rank(a) - rank(b));
  const index = Math.min(active, Math.max(0, matches.length - 1));
  const choose = (choice: Choice) => {
    if (disabled) return;
    setDismissed(true);
    onDraft('');
    input.current?.focus();
    choice.run();
    if (choice.id === 'command:model') setDismissed(false);
  };

  useEffect(() => {
    if (open) document.getElementById(`${id}-${index}`)?.scrollIntoView?.({ block: 'nearest' });
  }, [open, id, index, draft]);

  const submit = () => {
    if (!query || disabled || composing) return false;
    // A send-button click can dismiss the popup before submitting. Keep completion local.
    const choice = matches[index];
    if (choice) {
      choose(choice);
      return true;
    }
    if (open || !query.text || query.kind === 'model') {
      setDismissed(false);
      input.current?.focus();
      return true;
    }
    return false;
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || composing || event.keyCode === 229) return true;
    if (!open) return false;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setDismissed(true);
      return true;
    }
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !event.shiftKey) {
      event.preventDefault();
      setActive(
        matches.length
          ? (index + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length
          : 0,
      );
      return true;
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey) {
      if (event.key === 'Tab' && !matches.length) return false;
      event.preventDefault();
      submit();
      return true;
    }
    return false;
  };
  return {
    submit,
    onKeyDown,
    inputProps: {
      'aria-autocomplete': 'list' as const,
      'aria-controls': open ? id : undefined,
      'aria-activedescendant': open && matches.length ? `${id}-${index}` : undefined,
      onFocus: () => {
        setFocused(true);
        setDismissed(false);
      },
      onBlur: () => setFocused(false),
      onChange: (event: ChangeEvent<HTMLTextAreaElement>) => {
        setDismissed(false);
        onDraft(event.target.value);
      },
      onCompositionStart: () => setComposing(true),
      onCompositionEnd: () => setComposing(false),
    },
    menu:
      open &&
      createPortal(
        <div
          className="slash-menu"
          ref={popup}
          style={position}
          onPointerDown={(event) => event.preventDefault()}
        >
          <div className="slash-menu-heading">
            {query.kind === 'model' ? '选择模型' : '操作与技能'}
          </div>
          <div
            className="slash-menu-options"
            id={id}
            role="listbox"
            aria-label={query.kind === 'model' ? '选择模型' : '操作与技能'}
          >
            {matches.map((choice, i) => (
              <button
                key={choice.id}
                id={`${id}-${i}`}
                type="button"
                role="option"
                tabIndex={-1}
                aria-selected={index === i}
                onClick={() => choose(choice)}
              >
                <ChoiceIcon choice={choice} />
                <span className="slash-menu-label">
                  <strong>
                    {choice.kind === 'model' ? choice.description : `/${choice.name}`}
                  </strong>
                  <small>{choice.kind === 'model' ? choice.name : choice.description}</small>
                </span>
                {choice.selected && <Check size={14} aria-label="已选择" />}
                {choice.kind === 'skill' && <span className="slash-menu-kind">技能</span>}
              </button>
            ))}
            {!matches.length && !(loadSkills && (loading || error)) && (
              <p className="list-empty">
                没有匹配的{query.kind === 'model' ? '模型' : '操作或技能'}
              </p>
            )}
            {loadSkills && loading && (
              <p className="list-empty" role="status">
                正在读取技能…
              </p>
            )}
            {loadSkills && error && (
              <p className="list-empty" role="status">
                {error}
              </p>
            )}
          </div>
          <div className="slash-menu-footer">
            <span>↑ ↓ 选择</span>
            <span>
              <CornerDownLeft size={12} /> 确认
            </span>
            <span>Esc 关闭</span>
          </div>
        </div>,
        document.body,
      ),
  };
}
