import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { BookOpen, Search, X } from 'lucide-react';
import { skillsApi } from '../api';
import type { SkillSummary } from '../types';
import { usePickerPosition } from './usePickerPosition';

export function SkillsPicker({
  selected,
  onChange,
  disabled = false,
}: {
  selected: string[];
  onChange: (names: string[]) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLElement>(null);
  const position = usePickerPosition(open, anchor);
  const matching = skills.filter((skill) =>
    `${skill.name} ${skill.description}`.toLowerCase().includes(query.toLowerCase()),
  );
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!anchor.current?.contains(target) && !popover.current?.contains(target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.preventDefault();
      setOpen(false);
      anchor.current?.focus();
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    void skillsApi
      .list()
      .then((result) => {
        if (active) {
          setSkills(result.skills);
          setError('');
        }
      })
      .catch((error) => {
        if (active) setError(error.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open]);
  return (
    <div className="skills-picker">
      <button
        ref={anchor}
        type="button"
        className="workspace-button skills-picker-trigger"
        disabled={disabled}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        data-selected={selected.length > 0}
      >
        <BookOpen size={15} />
        <span>技能{selected.length ? ` ${selected.length}` : ''}</span>
      </button>
      {open &&
        createPortal(
          <section
            ref={popover}
            className="composer-picker skills-picker-popover"
            style={position}
            role="dialog"
            aria-label="选择技能"
          >
            <header className="picker-heading">
              <div>
                <strong>选择技能</strong>
                <small>可多选</small>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭技能选择"
                onClick={() => {
                  setOpen(false);
                  anchor.current?.focus();
                }}
              >
                <X size={15} />
              </button>
            </header>
            <label className="picker-search">
              <Search size={14} />
              <input
                aria-label="查找技能"
                placeholder="查找已安装的技能"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            {loading ? (
              <p className="list-empty">正在加载…</p>
            ) : (
              <div className="skills-picker-options">
                {matching.map((skill) => (
                  <label
                    key={skill.name}
                    className={selected.includes(skill.name) ? 'is-selected' : ''}
                  >
                    <span className="picker-item-icon">
                      <BookOpen size={17} />
                    </span>
                    <input
                      type="checkbox"
                      disabled={disabled || (!skill.enabled && !selected.includes(skill.name))}
                      checked={selected.includes(skill.name)}
                      onChange={(e) =>
                        onChange(
                          e.target.checked
                            ? [...selected, skill.name]
                            : selected.filter((name) => name !== skill.name),
                        )
                      }
                    />
                    <span>
                      <strong>{skill.name}</strong>
                      <small>{skill.enabled ? skill.description : '已停用'}</small>
                    </span>
                  </label>
                ))}
                {!matching.length && (
                  <div className="picker-empty">
                    <BookOpen size={22} />
                    <strong>{skills.length ? '没有找到匹配的技能' : '还没有安装技能'}</strong>
                    <span>{skills.length ? '换个名称试试' : '可在技能页添加'}</span>
                  </div>
                )}
              </div>
            )}
            <footer className="picker-footer">
              <span>{selected.length ? `已选 ${selected.length} 项` : '尚未选择'}</span>
              {selected.length > 0 && (
                <button type="button" disabled={disabled} onClick={() => onChange([])}>
                  清空
                </button>
              )}
            </footer>
          </section>,
          document.body,
        )}
    </div>
  );
}
