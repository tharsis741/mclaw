import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  chooseWelcome,
  readLastWelcome,
  rememberWelcome,
  WELCOME_ACTIONS,
} from './welcome-content';
import { LAYOUT_NAMES } from './components/companion-layouts';

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

it('draws six different, usable actions from a twelve-action pool', () => {
  expect(WELCOME_ACTIONS).toHaveLength(12);
  expect(new Set(WELCOME_ACTIONS.map(({ id }) => id)).size).toBe(12);
  const original = WELCOME_ACTIONS.map(({ id }) => id);
  for (const random of [() => 0, () => 0.999, Math.random]) {
    const selection = chooseWelcome(null, random);
    expect(selection.actions).toHaveLength(6);
    expect(new Set(selection.actions.map(({ id }) => id)).size).toBe(6);
    expect(LAYOUT_NAMES).toContain(selection.layout);
    for (const action of selection.actions) {
      expect(action.title).toBeTruthy();
      expect(action.text).toBeTruthy();
      expect(action.icon).toBeTruthy();
    }
  }
  expect(WELCOME_ACTIONS.map(({ id }) => id)).toEqual(original);
});

it('changes the assortment and layout even when the same random draw repeats', () => {
  const first = chooseWelcome(null, () => 0);
  rememberWelcome(first);
  const next = chooseWelcome(readLastWelcome(), () => 0);
  expect(next.layout).not.toBe(first.layout);
  expect(next.actions.map(({ id }) => id).sort()).not.toEqual(
    first.actions.map(({ id }) => id).sort(),
  );
  expect(new Set(next.actions.map(({ id }) => id)).size).toBe(6);
});

it.each(['{', 'null', '{"actionIds":[2],"expression":"neutre"}', '{"actionIds":[]}'])(
  'ignores invalid welcome preferences: %s',
  (stored) => {
    localStorage.setItem('mclaw.welcome.v1', stored);
    expect(readLastWelcome()).toBeNull();
    expect(chooseWelcome(readLastWelcome()).actions).toHaveLength(6);
  },
);

it('retains the previous shortcuts from legacy welcome preferences', () => {
  const actionIds = chooseWelcome(null, () => 0).actions.map((action) => action.id);
  localStorage.setItem('mclaw.welcome.v1', JSON.stringify({ actionIds, expression: 'neutre' }));
  const previous = readLastWelcome();
  expect(previous?.actionIds).toEqual(actionIds);
  const next = chooseWelcome(previous, () => 0);
  expect(next.actions.map((action) => action.id)).not.toEqual(actionIds);
  rememberWelcome(next);
  expect(readLastWelcome()?.layout).toBe(next.layout);
});

it('still opens the welcome when browser storage is blocked', () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new Error('blocked');
  });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('blocked');
  });
  expect(readLastWelcome()).toBeNull();
  expect(() => rememberWelcome(chooseWelcome(null))).not.toThrow();
});

it.each([
  ['bridge', 'portrait'],
  ['balance', 'cascade'],
  ['stack', 'stack'],
  ['circle', 'circle'],
])(
  'upgrades the %s geometry preference to %s while retaining its shortcuts',
  (oldLayout, nextLayout) => {
    const actionIds = ['files', 'research', 'document', 'table', 'writing', 'translate'];
    localStorage.setItem('mclaw.welcome.v1', JSON.stringify({ actionIds, layout: oldLayout }));
    expect(readLastWelcome()).toEqual({ actionIds, layout: nextLayout });
    expect(chooseWelcome(readLastWelcome()).layout).not.toBe(nextLayout);
  },
);
