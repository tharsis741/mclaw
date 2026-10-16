import { act, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useStartupIntro } from './useStartupIntro';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it('finishes the entrance and crossfade before releasing a fast startup', () => {
  vi.useFakeTimers();
  vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList);
  const { result } = renderHook(() => useStartupIntro(true));
  act(() => vi.advanceTimersByTime(1199));
  expect(result.current).toEqual({ visible: true, leaving: false });
  act(() => vi.advanceTimersByTime(1));
  expect(result.current).toEqual({ visible: true, leaving: true });
  act(() => vi.advanceTimersByTime(240));
  expect(result.current.visible).toBe(false);
});

it('waits for the service, then crossfades without replaying the entrance', () => {
  vi.useFakeTimers();
  vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList);
  const { result, rerender, unmount } = renderHook(({ ready }) => useStartupIntro(ready), {
    initialProps: { ready: false },
  });
  act(() => vi.advanceTimersByTime(5000));
  expect(result.current).toEqual({ visible: true, leaving: false });
  rerender({ ready: true });
  expect(result.current).toEqual({ visible: true, leaving: true });
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it('respects reduced motion without holding the user on the startup screen', () => {
  vi.useFakeTimers();
  const { result, rerender } = renderHook(({ ready }) => useStartupIntro(ready), {
    initialProps: { ready: false },
  });
  expect(result.current.visible).toBe(true);
  rerender({ ready: true });
  expect(result.current.visible).toBe(false);
  expect(vi.getTimerCount()).toBe(0);
});
