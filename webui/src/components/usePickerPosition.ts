import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

/** Keep the content-sized picker next to its trigger, including above the composer. */
export function usePickerPosition(
  open: boolean,
  anchor: RefObject<HTMLElement | null>,
  {
    width: preferredWidth = 320,
    maxHeight = 360,
    align = 'start',
  }: { width?: number; maxHeight?: number; align?: 'start' | 'end' } = {},
) {
  const [position, setPosition] = useState<CSSProperties>({ visibility: 'hidden' });
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const bounds = anchor.current?.getBoundingClientRect();
      if (!bounds) return;
      const viewport = window.visualViewport;
      const left = viewport?.offsetLeft ?? 0;
      const top = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? window.innerWidth;
      const height = viewport?.height ?? window.innerHeight;
      const above = bounds.top - top - 20;
      const below = top + height - bounds.bottom - 20;
      const upwards = above >= 240 || above > below;
      const popupWidth = Math.min(preferredWidth, width - 24);
      setPosition({
        position: 'fixed',
        zIndex: 120,
        width: popupWidth,
        left: Math.max(
          left + 12,
          Math.min(
            align === 'end' ? bounds.right - popupWidth : bounds.left,
            left + width - popupWidth - 12,
          ),
        ),
        ...(upwards
          ? { bottom: window.innerHeight - bounds.top + 8, top: 'auto' }
          : { top: bounds.bottom + 8, bottom: 'auto' }),
        maxHeight: Math.max(80, Math.min(maxHeight, upwards ? above : below)),
      });
    };
    place();
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
  }, [open, anchor, preferredWidth, maxHeight, align]);
  return position;
}
