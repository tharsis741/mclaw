import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
afterEach(cleanup);
Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }),
});
Element.prototype.scrollIntoView = () => {};
