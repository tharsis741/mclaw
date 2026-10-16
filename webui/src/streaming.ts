import type { Session } from './types';

export interface SnapshotChange {
  op: 'set' | 'append' | 'remove';
  path: (string | number)[];
  value?: unknown;
}

// Only the changed branches get new references, so completed messages can skip rendering.
export function applySnapshotChanges(session: Session, changes: SnapshotChange[]): Session {
  function update(node: unknown, change: SnapshotChange, depth: number): unknown {
    if (depth === change.path.length) {
      if (change.op === 'set') return change.value;
      if (change.op === 'append') {
        if (typeof node === 'string' && typeof change.value === 'string')
          return node + change.value;
        if (Array.isArray(node) && Array.isArray(change.value)) return [...node, ...change.value];
      }
      throw new Error('Invalid snapshot change');
    }
    const key = change.path[depth];
    if (['__proto__', 'prototype', 'constructor'].includes(String(key)))
      throw new Error('Invalid snapshot path');
    if (!node || typeof node !== 'object') throw new Error('Missing snapshot path');
    const copy = (Array.isArray(node) ? [...node] : { ...node }) as Record<
      string | number,
      unknown
    >;
    if (depth === change.path.length - 1 && change.op === 'remove') delete copy[key];
    else copy[key] = update(copy[key], change, depth + 1);
    return copy;
  }
  return changes.reduce((value, change) => update(value, change, 0) as Session, session);
}
