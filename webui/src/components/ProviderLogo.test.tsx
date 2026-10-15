import { fireEvent, render } from '@testing-library/react';
import { expect, it } from 'vitest';
import { ProviderLogo } from './ProviderLogo';

it('recovers from a failed logo when the provider changes', () => {
  const view = render(<ProviderLogo provider="moonshot" />);
  const first = view.container.querySelector('img')!;
  fireEvent.error(first);
  expect(view.container.querySelector('img')).toBeNull();
  expect(view.container.querySelector('svg')).toBeInTheDocument();

  view.rerender(<ProviderLogo provider="deepseek" />);
  expect(view.container.querySelector('img')).toHaveAttribute(
    'src',
    '/provider-logos/deepseek.svg',
  );
  expect(view.container.querySelector('svg')).toBeNull();
});

it('uses a local fallback for an unknown provider without requesting an invalid asset', () => {
  const view = render(<ProviderLogo provider="custom-private-provider" />);
  expect(view.container.querySelector('img')).toBeNull();
  expect(view.container.querySelector('svg')).toBeInTheDocument();
});
