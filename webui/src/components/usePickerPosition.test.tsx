import { useRef } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { usePickerPosition } from './usePickerPosition';

function Picker({ open = true }: { open?: boolean }) {
  const anchor = useRef<HTMLButtonElement>(null);
  const style = usePickerPosition(open, anchor);
  return (
    <>
      <button ref={anchor}>选择</button>
      {open && (
        <section role="dialog" style={style}>
          内容
        </section>
      )}
    </>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('composer picker placement', () => {
  it('anchors the bottom edge above the trigger instead of leaving a gap for short content', () => {
    vi.stubGlobal('innerWidth', 1280);
    vi.stubGlobal('innerHeight', 720);
    const bounds = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      left: 350,
      right: 410,
      top: 580,
      bottom: 616,
    } as DOMRect);
    render(<Picker />);
    expect(screen.getByRole('dialog')).toHaveStyle({ bottom: '148px', top: 'auto' });
    bounds.mockReturnValue({ left: 350, right: 410, top: 510, bottom: 546 } as DOMRect);
    fireEvent.scroll(window);
    expect(screen.getByRole('dialog')).toHaveStyle({ bottom: '218px' });
  });

  it('opens below a trigger near the top and fits inside a narrow viewport', () => {
    vi.stubGlobal('innerWidth', 320);
    vi.stubGlobal('innerHeight', 640);
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      left: 260,
      right: 308,
      top: 30,
      bottom: 66,
    } as DOMRect);
    render(<Picker />);
    expect(screen.getByRole('dialog')).toHaveStyle({
      top: '74px',
      bottom: 'auto',
      left: '12px',
      width: '296px',
    });
  });
});
