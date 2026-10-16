import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { WelcomeCompanion } from './WelcomeCompanion';

let media: EventTarget & { matches: boolean };
beforeEach(() => {
  vi.useFakeTimers();
  media = Object.assign(new EventTarget(), { matches: false });
  vi.stubGlobal('matchMedia', () => media);
  vi.spyOn(Math, 'random').mockReturnValue(0);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
const advance = (ms: number) => act(() => vi.advanceTimersByTime(ms));
const actor = () => screen.getByRole('button', { name: '和字母小伙伴打个招呼' });
const piece = (letter: string) => actor().querySelector<HTMLElement>(`[data-letter="${letter}"]`)!;
const art = () => Array.from(actor().querySelectorAll('img[data-sprite]'));
const loadArt = () => art().forEach((image) => fireEvent.load(image));

it('ships five letter sprites and waits for both assets and startup before opening the door', () => {
  const raf = vi.spyOn(window, 'requestAnimationFrame');
  const view = render(
    <StrictMode>
      <WelcomeCompanion initialLayout="portrait" active={false} />
    </StrictMode>,
  );
  expect(actor().querySelectorAll('[data-letter]')).toHaveLength(5);
  expect(art()).toHaveLength(7);
  expect(art().map((image) => image.getAttribute('src'))).toContain(
    '/welcome-letters/letter-w.png',
  );
  expect(actor().querySelector('[data-piece]')).toBeNull();
  loadArt();
  expect(actor()).toBeDisabled();
  expect(raf).not.toHaveBeenCalled();
  view.rerender(
    <StrictMode>
      <WelcomeCompanion initialLayout="portrait" />
    </StrictMode>,
  );
  expect(actor()).toHaveAttribute('data-scene', 'arrival');
  advance(600);
  expect(Number(piece('M').style.opacity)).toBeGreaterThan(0);
  expect(piece('C').style.opacity).toBe('0');
  expect(piece('W').style.opacity).toBe('0');
  advance(1400);
  expect(Number(piece('W').style.opacity)).toBeGreaterThan(0);
  advance(2000);
  expect(actor()).toHaveAttribute('data-scene', 'idle');
  expect(actor().querySelector<HTMLElement>('[data-door]')!.style.opacity).toBe('0');
  raf.mockClear();
  advance(500);
  expect(raf).not.toHaveBeenCalled();
});

it('waits for the last sprite and falls back to the brand if an asset fails', () => {
  const view = render(<WelcomeCompanion initialLayout="line" />);
  art()
    .slice(0, -1)
    .forEach((image) => fireEvent.load(image));
  advance(1000);
  expect(actor()).toHaveAttribute('data-ready', 'false');
  expect(actor()).toHaveAttribute('data-scene', 'idle');
  fireEvent.load(art().at(-1)!);
  expect(actor()).toHaveAttribute('data-scene', 'arrival');
  fireEvent.error(art()[0]);
  expect(actor()).toHaveAttribute('data-asset-error', 'true');
  expect(actor()).toHaveAttribute('data-animating', 'false');
  expect(actor().querySelector('.letter-fallback')).toHaveAttribute(
    'src',
    '/mclaw-wordmark-fold.svg',
  );
  expect(vi.getTimerCount()).toBe(0);
  view.unmount();
});

it.each(['M', 'C', 'L', 'A', 'W'])(
  'lets the group notice %s and varies repeated greetings',
  (letter) => {
    render(<WelcomeCompanion initialLayout="stack" />);
    loadArt();
    advance(4000);
    fireEvent.click(piece(letter).firstElementChild!);
    advance(400);
    expect(actor()).toHaveAttribute('data-scene', 'tap');
    for (const other of ['M', 'C', 'L', 'A', 'W'])
      expect(piece(other)).toHaveAttribute('data-attention', letter);
    const first = piece(letter).style.transform;
    advance(1100);
    expect(actor()).toHaveAttribute('data-scene', 'idle');
    fireEvent.click(piece(letter));
    advance(400);
    expect(piece(letter).style.transform).not.toBe(first);
  },
);

it('supports native keyboard activation without adding animation controls', () => {
  render(<WelcomeCompanion initialLayout="pairs" />);
  loadArt();
  advance(4000);
  fireEvent.click(actor(), { detail: 0 });
  expect(actor()).toHaveAttribute('data-scene', 'tap');
  expect(piece('M')).toHaveAttribute('data-attention', 'W');
  expect(screen.queryByRole('slider')).not.toBeInTheDocument();
  expect(screen.getAllByRole('button')).toHaveLength(1);
  expect(screen.queryByText('重播出场')).not.toBeInTheDocument();
  expect(screen.queryByText('换个组合')).not.toBeInTheDocument();
});

it('settles while writing, resumes quietly, and releases all scheduled work on unmount', () => {
  const view = render(<WelcomeCompanion initialLayout="portrait" />);
  loadArt();
  advance(900);
  view.rerender(<WelcomeCompanion initialLayout="portrait" quiet />);
  expect(actor()).toBeDisabled();
  expect(actor()).toHaveAttribute('data-scene', 'idle');
  expect(vi.getTimerCount()).toBe(0);
  view.rerender(<WelcomeCompanion initialLayout="portrait" />);
  expect(actor()).toBeEnabled();
  expect(actor()).toHaveAttribute('data-scene', 'idle');
  fireEvent.click(piece('M'));
  const image = art()[0];
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
  fireEvent.load(image);
  expect(vi.getTimerCount()).toBe(0);
});

it('never replays an entrance skipped for a draft or reduced motion', () => {
  const view = render(<WelcomeCompanion initialLayout="stack" quiet />);
  loadArt();
  expect(vi.getTimerCount()).toBe(0);
  view.rerender(<WelcomeCompanion initialLayout="stack" />);
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  fireEvent(document, new Event('visibilitychange'));
  hidden.mockReturnValue(false);
  fireEvent(document, new Event('visibilitychange'));
  expect(actor()).toHaveAttribute('data-scene', 'idle');
});

it('stops frames and timers when hidden or offscreen and disconnects observers', () => {
  let intersect!: IntersectionObserverCallback;
  const disconnect = vi.fn();
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(callback: IntersectionObserverCallback) {
        intersect = callback;
      }
      observe() {}
      disconnect = disconnect;
    },
  );
  const view = render(<WelcomeCompanion initialLayout="circle" />);
  loadArt();
  advance(1000);
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  fireEvent(document, new Event('visibilitychange'));
  expect(vi.getTimerCount()).toBe(0);
  hidden.mockReturnValue(false);
  fireEvent(document, new Event('visibilitychange'));
  expect(actor()).toHaveAttribute('data-scene', 'idle');
  expect(vi.getTimerCount()).toBe(1);
  act(() =>
    intersect([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver),
  );
  expect(vi.getTimerCount()).toBe(0);
  act(() =>
    intersect([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
  );
  expect(vi.getTimerCount()).toBe(1);
  view.unmount();
  expect(disconnect).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

it('honors a change to reduced motion immediately and still provides an accessible greeting', () => {
  render(<WelcomeCompanion initialLayout="portrait" />);
  loadArt();
  advance(1000);
  act(() => {
    media.matches = true;
    media.dispatchEvent(new Event('change'));
  });
  const raf = vi.spyOn(window, 'requestAnimationFrame');
  fireEvent.click(piece('C'));
  expect(actor()).toHaveAttribute('data-animating', 'false');
  expect(actor().querySelector('[data-announcement]')).toHaveTextContent('和 C 打个招呼');
  expect(raf).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});
