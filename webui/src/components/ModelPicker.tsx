import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Grid2X2, Search, Settings2, X } from 'lucide-react';
import type { ModelOption } from '../types';
import { ProviderLogo } from './ProviderLogo';
import { usePickerPosition } from './usePickerPosition';

function runtimeProvider(model: ModelOption) {
  return model.provider || (model.id.includes('::') ? model.id.split('::')[0] : '');
}

function providerOf(model: ModelOption) {
  const provider = runtimeProvider(model);
  return (
    (
      { 'qwen-intl': 'qwen', 'moonshot-intl': 'moonshot', 'minimax-cn': 'minimax' } as Record<
        string,
        string
      >
    )[provider] || provider
  );
}

function brandLabel(model: ModelOption) {
  return (
    ({ qwen: '通义千问', moonshot: 'Kimi', minimax: 'MiniMax' } as Record<string, string>)[
      providerOf(model)
    ] ||
    model.provider_label ||
    providerOf(model) ||
    '其他模型'
  );
}

function regionLabel(model: ModelOption) {
  return (
    (
      {
        qwen: '中国区',
        'qwen-intl': '国际区',
        moonshot: '中国区',
        'moonshot-intl': '国际区',
        'minimax-cn': '中国区',
        minimax: '国际区',
      } as Record<string, string>
    )[runtimeProvider(model)] || ''
  );
}

function matches(model: ModelOption, label: string, query: string) {
  return `${label} ${model.provider_label || ''} ${runtimeProvider(model)} ${regionLabel(model)} ${model.label}`
    .toLocaleLowerCase()
    .includes(query.trim().toLocaleLowerCase());
}

export function ModelPicker({
  models,
  value,
  disabled,
  onChange,
  onManage,
}: {
  models: ModelOption[];
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  onManage?: () => void;
}) {
  const id = useId();
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [provider, setProvider] = useState<string | null>(null);
  const [active, setActive] = useState(value);
  const position = usePickerPosition(open, anchor, { width: 480, maxHeight: 490, align: 'end' });
  const choices = useMemo(() => {
    if (!value || models.some((model) => model.id === value)) return models;
    return [
      { id: value, label: value.includes('::') ? value.split('::').slice(1).join('::') : value },
      ...models,
    ];
  }, [models, value]);
  const groups = useMemo(() => {
    const result = new Map<string, { id: string; label: string; models: ModelOption[] }>();
    for (const model of choices) {
      const key = providerOf(model);
      if (!result.has(key)) result.set(key, { id: key, label: brandLabel(model), models: [] });
      const group = result.get(key)!;
      if (model.provider_label) group.label = brandLabel(model);
      group.models.push(model);
    }
    return [...result.values()];
  }, [choices]);
  const selected = choices.find((model) => model.id === value);
  const selectedProvider = selected ? providerOf(selected) : '';
  const matching = groups
    .filter((group) => provider === null || group.id === provider)
    .map((group) => ({
      ...group,
      models: group.models.filter((model) => matches(model, group.label, query)),
    }))
    .filter((group) => group.models.length);
  const visible = matching.flatMap((group) => group.models);
  const activeIndex = visible.findIndex((model) => model.id === active);
  const activeId = open && activeIndex >= 0 ? `${id}-option-${activeIndex}` : undefined;
  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) anchor.current?.focus({ preventScroll: true });
  };
  const show = () => {
    if (disabled) return;
    setQuery('');
    setProvider(null);
    setActive(value || choices[0]?.id || '');
    setOpen(true);
  };
  const choose = (model: ModelOption) => {
    if (disabled) return;
    onChange(model.id);
    close(true);
  };
  useEffect(() => {
    if (!open) return;
    if (disabled) return close();
    search.current?.focus({ preventScroll: true });
    const outside = (event: Event) => {
      if (
        !anchor.current?.contains(event.target as Node) &&
        !panel.current?.contains(event.target as Node)
      )
        close();
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('focusin', outside);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('focusin', outside);
    };
  }, [open, disabled]);
  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView({ block: 'nearest' });
  }, [activeId]);
  const keyboard = (event: KeyboardEvent<HTMLElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!open) return show();
      const next =
        event.key === 'ArrowDown'
          ? activeIndex + 1
          : activeIndex < 0
            ? visible.length - 1
            : activeIndex - 1;
      setActive(visible[(next + visible.length) % visible.length]?.id ?? '');
    } else if (event.key === 'Enter' && event.target === search.current) {
      event.preventDefault();
      event.stopPropagation();
      if (activeIndex >= 0) choose(visible[activeIndex]);
    }
  };
  return (
    <>
      <button
        type="button"
        ref={anchor}
        className="model-picker-trigger"
        role="combobox"
        aria-label="会话模型"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? `${id}-panel` : undefined}
        disabled={disabled}
        onClick={() => (open ? close(true) : show())}
        onKeyDown={keyboard}
        title={
          selected
            ? [
                groups.find((group) => group.id === selectedProvider)?.label,
                regionLabel(selected),
                selected.label,
              ]
                .filter(Boolean)
                .join(' · ')
            : '选择模型'
        }
      >
        {selectedProvider && <ProviderLogo provider={selectedProvider} />}
        <span>{selected?.label || '默认模型'}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open &&
        createPortal(
          <section
            ref={panel}
            id={`${id}-panel`}
            role="dialog"
            aria-label="选择模型"
            className="composer-picker model-picker-popover"
            style={position}
            onKeyDown={keyboard}
            onClick={(event) => event.stopPropagation()}
          >
            <header className="picker-heading">
              <div>
                <strong>选择模型</strong>
                <small>
                  {groups.length} 个供应商 · {choices.length} 个模型
                </small>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭模型选择"
                onClick={() => close(true)}
              >
                <X size={16} />
              </button>
            </header>
            <label className="picker-search">
              <Search size={16} aria-hidden="true" />
              <input
                ref={search}
                type="search"
                aria-label="搜索会话模型"
                placeholder="搜索模型或供应商"
                value={query}
                aria-controls={`${id}-list`}
                aria-activedescendant={activeId}
                autoComplete="off"
                onChange={(event) => {
                  const next = event.target.value;
                  setQuery(next);
                  setProvider(null);
                  setActive(
                    groups.flatMap((group) =>
                      group.models.filter((model) => matches(model, group.label, next)),
                    )[0]?.id ?? '',
                  );
                }}
              />
            </label>
            <div className="model-picker-body">
              <nav className="model-provider-list" aria-label="模型供应商">
                <button
                  type="button"
                  aria-pressed={provider === null}
                  onClick={() => {
                    setProvider(null);
                    setQuery('');
                    setActive(value || choices[0]?.id || '');
                  }}
                >
                  <Grid2X2 size={17} />
                  <span>全部</span>
                  <small>{choices.length}</small>
                </button>
                {groups.map((group) => (
                  <button
                    key={group.id}
                    type="button"
                    title={group.label}
                    aria-pressed={provider === group.id}
                    onClick={() => {
                      setProvider(group.id);
                      setQuery('');
                      setActive(
                        group.models.find((model) => model.id === value)?.id || group.models[0].id,
                      );
                    }}
                  >
                    <ProviderLogo provider={group.id} />
                    <span>{group.label}</span>
                    <small>{group.models.length}</small>
                  </button>
                ))}
              </nav>
              <div
                className="model-choice-list"
                id={`${id}-list`}
                role="listbox"
                aria-label="会话模型"
              >
                {matching.map((group) => (
                  <div
                    key={group.id}
                    role="group"
                    aria-label={group.label}
                    className="model-choice-group"
                  >
                    <div className="model-group-heading">{group.label}</div>
                    {group.models.map((model) => (
                      <div
                        key={model.id}
                        id={`${id}-option-${visible.indexOf(model)}`}
                        role="option"
                        aria-selected={model.id === value}
                        className={`model-choice${model.id === active ? ' is-active' : ''}`}
                        onPointerMove={() => setActive(model.id)}
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => choose(model)}
                      >
                        <span>
                          {model.label}
                          {regionLabel(model) && <small>{regionLabel(model)}</small>}
                          {!models.some((option) => option.id === model.id) && (
                            <small>会话已保存</small>
                          )}
                        </span>
                        {model.id === value && <Check size={16} aria-hidden="true" />}
                      </div>
                    ))}
                  </div>
                ))}
                {!visible.length && (
                  <div className="picker-empty">
                    <Search size={22} />
                    <strong>{choices.length ? '没有找到匹配的模型' : '还没有可用模型'}</strong>
                    <span>{choices.length ? '换个名称试试' : '添加供应商后即可选择'}</span>
                  </div>
                )}
              </div>
            </div>
            {onManage && (
              <footer className="picker-footer">
                <button
                  type="button"
                  className="picker-manage"
                  onClick={() => {
                    close(true);
                    onManage();
                  }}
                >
                  <Settings2 size={14} />
                  管理供应商
                </button>
              </footer>
            )}
          </section>,
          document.body,
        )}
    </>
  );
}
