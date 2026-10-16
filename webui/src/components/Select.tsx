import {
  Children,
  Fragment,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Search } from 'lucide-react';

type Option = { value: string; label: string; disabled: boolean };
type SelectProps = {
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  children: ReactNode;
  disabled?: boolean;
  searchable?: boolean;
  editable?: boolean;
  required?: boolean;
  placeholder?: string;
  className?: string;
};

function textContent(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) =>
      isValidElement<{ children?: ReactNode }>(child)
        ? textContent(child.props.children)
        : String(child),
    )
    .join('');
}

function readOptions(children: ReactNode): Option[] {
  return Children.toArray(children).flatMap((child) => {
    if (
      !isValidElement<{ value?: string | number; disabled?: boolean; children?: ReactNode }>(child)
    )
      return [];
    if (child.type === Fragment) return readOptions(child.props.children);
    if (child.type !== 'option') return [];
    const label = textContent(child.props.children) || String(child.props.value ?? '');
    return [{ value: String(child.props.value ?? label), label, disabled: !!child.props.disabled }];
  });
}

/** Shared single-choice control, including editable model and timezone suggestions. */
export function Select({
  label,
  value,
  onChange,
  children,
  disabled = false,
  searchable = false,
  editable = false,
  required = false,
  placeholder = '请选择',
  className = '',
}: SelectProps) {
  const id = useId();
  const trigger = useRef<HTMLElement>(null);
  const container = useRef<HTMLSpanElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const typed = useRef({ text: '', at: 0 });
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState<string | null>(null);
  const [position, setPosition] = useState<CSSProperties>({});
  const options = readOptions(children);
  const selected = options.find((option) => option.value === String(value));
  const filtered = options.filter((option) =>
    option.label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );
  const available = filtered.filter((option) => !option.disabled);
  const activeIndex = filtered.findIndex((option) => option.value === active && !option.disabled);
  const listId = `${id}-list`;
  const activeId = activeIndex >= 0 ? `${id}-option-${activeIndex}` : undefined;

  const close = (restoreFocus = false) => {
    setOpen(false);
    setQuery('');
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  };
  const show = (last = false) => {
    if (disabled || trigger.current?.matches(':disabled')) return;
    setQuery('');
    setActive(
      selected && !selected.disabled
        ? selected.value
        : editable
          ? null
          : (options.filter((option) => !option.disabled).at(last ? -1 : 0)?.value ?? null),
    );
    setOpen(true);
  };
  const choose = (option: Option) => {
    if (option.disabled || disabled || trigger.current?.matches(':disabled')) return;
    onChange(option.value);
    close(true);
  };

  useLayoutEffect(() => {
    if (!open) return;
    if (disabled || trigger.current?.matches(':disabled')) {
      setOpen(false);
      return;
    }
    const place = () => {
      const rect = container.current?.getBoundingClientRect();
      if (!rect) return;
      const viewport = window.visualViewport;
      const leftEdge = viewport?.offsetLeft ?? 0;
      const topEdge = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? window.innerWidth;
      const height = viewport?.height ?? window.innerHeight;
      const below = topEdge + height - rect.bottom - 20;
      const above = rect.top - topEdge - 20;
      const upwards = below < 260 && above > below;
      const popupWidth = Math.min(Math.max(rect.width, searchable ? 300 : 240), width - 24);
      setPosition({
        width: popupWidth,
        left: Math.max(leftEdge + 12, Math.min(rect.left, leftEdge + width - popupWidth - 12)),
        ...(upwards ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }),
        maxHeight: Math.max(80, Math.min(336, upwards ? above : below)),
      });
    };
    place();
    if (searchable && !editable) search.current?.focus({ preventScroll: true });
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    window.visualViewport?.addEventListener('resize', place);
    window.visualViewport?.addEventListener('scroll', place);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      window.visualViewport?.removeEventListener('resize', place);
      window.visualViewport?.removeEventListener('scroll', place);
    };
  }, [open, disabled, searchable, editable]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      const target = event.target as Node;
      if (!container.current?.contains(target) && !popup.current?.contains(target)) {
        setOpen(false);
        setQuery('');
      }
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('focusin', outside);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('focusin', outside);
    };
  }, [open]);

  useEffect(() => {
    if (open && activeId) document.getElementById(activeId)?.scrollIntoView({ block: 'nearest' });
  }, [open, activeId]);

  const keyboard = (event: KeyboardEvent<HTMLElement>) => {
    if (event.nativeEvent.isComposing) return;
    const isInput = event.target instanceof HTMLInputElement;
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'Tab' && open) {
      // Return from the portalled search field before normal form tab navigation.
      close(event.target === search.current);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      event.stopPropagation();
      if (!open) return show(event.key === 'ArrowUp');
      const index = available.findIndex((option) => option.value === active);
      const next =
        event.key === 'ArrowDown' ? index + 1 : index < 0 ? available.length - 1 : index - 1;
      setActive(available[(next + available.length) % available.length]?.value ?? null);
    } else if ((event.key === 'Home' || event.key === 'End') && open && !isInput) {
      event.preventDefault();
      setActive(available.at(event.key === 'End' ? -1 : 0)?.value ?? null);
    } else if (event.key === 'Enter' || (event.key === ' ' && !isInput)) {
      if (!open && !editable) {
        event.preventDefault();
        show();
      } else if (open) {
        if (editable && activeIndex < 0) return close();
        event.preventDefault();
        event.stopPropagation();
        if (activeIndex >= 0) choose(filtered[activeIndex]);
      }
    } else if (
      event.key.length === 1 &&
      !isInput &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      event.preventDefault();
      const now = Date.now();
      typed.current = {
        text: (now - typed.current.at < 600 ? typed.current.text : '') + event.key,
        at: now,
      };
      const prefix = typed.current.text.toLocaleLowerCase();
      const match = options.find(
        (option) => !option.disabled && option.label.toLocaleLowerCase().startsWith(prefix),
      );
      if (!open) show();
      if (match) setActive(match.value);
    }
  };

  const attributes = {
    role: 'combobox',
    'aria-label': label,
    'aria-expanded': open,
    'aria-haspopup': 'listbox' as const,
    'aria-controls': open ? listId : undefined,
    'aria-activedescendant': open ? activeId : undefined,
    disabled,
    onKeyDown: keyboard,
  };

  return (
    <span
      className={`select-control ${editable ? 'select-editable' : ''} ${className}`}
      ref={container}
    >
      {editable ? (
        <>
          <input
            {...attributes}
            ref={(element) => {
              trigger.current = element;
            }}
            className="select-input"
            aria-autocomplete="list"
            value={value}
            required={required}
            placeholder={placeholder}
            autoComplete="off"
            onClick={() => {
              if (!open) show();
            }}
            onChange={(event) => {
              onChange(event.target.value);
              setQuery(event.target.value);
              setActive(null);
              setOpen(true);
            }}
          />
          <button
            type="button"
            className="select-toggle"
            aria-label={`${label}选项`}
            tabIndex={-1}
            disabled={disabled}
            onClick={() => {
              trigger.current?.focus();
              if (open) close();
              else show();
            }}
          >
            <ChevronDown size={15} aria-hidden="true" />
          </button>
        </>
      ) : (
        <button
          {...attributes}
          type="button"
          className="select-trigger"
          ref={(element) => {
            trigger.current = element;
          }}
          onClick={() => {
            if (open) close(true);
            else show();
          }}
        >
          <span className={!selected && !value ? 'select-placeholder' : ''}>
            {selected?.label ?? (String(value) || placeholder)}
          </span>
          <ChevronDown size={15} aria-hidden="true" />
        </button>
      )}
      {open &&
        createPortal(
          <div
            className="select-popup"
            ref={popup}
            style={position}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={keyboard}
          >
            {searchable && !editable && (
              <div className="select-search">
                <Search size={15} aria-hidden="true" />
                <input
                  ref={search}
                  type="search"
                  aria-label={`搜索${label}`}
                  aria-controls={listId}
                  aria-activedescendant={activeId}
                  autoComplete="off"
                  placeholder="搜索…"
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    setActive(
                      options.find(
                        (option) =>
                          !option.disabled &&
                          option.label
                            .toLocaleLowerCase()
                            .includes(event.target.value.trim().toLocaleLowerCase()),
                      )?.value ?? null,
                    );
                  }}
                />
              </div>
            )}
            <div className="select-options" role="listbox" id={listId} aria-label={label}>
              {filtered.map((option, index) => (
                <div
                  key={option.value}
                  id={`${id}-option-${index}`}
                  role="option"
                  aria-selected={option.value === String(value)}
                  aria-disabled={option.disabled || undefined}
                  className={`select-option ${index === activeIndex ? 'is-active' : ''}`}
                  onPointerMove={() => {
                    if (!option.disabled) setActive(option.value);
                  }}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => choose(option)}
                >
                  <span>{option.label}</span>
                  {option.value === String(value) && <Check size={15} aria-hidden="true" />}
                </div>
              ))}
            </div>
            {!filtered.length && (
              <p className="select-empty">{editable ? '可直接使用输入的内容' : '没有匹配项'}</p>
            )}
          </div>,
          document.body,
        )}
    </span>
  );
}
