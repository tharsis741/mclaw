import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { RemoteExecution } from './RemoteExecution';

it('defers a complete long remote reply until expanded, independently of clipped technical JSON', () => {
  const text = '完整正文\n\n'.repeat(12000) + '最后一段仍然可见';
  const view = render(
    <RemoteExecution
      entry={{
        id: 'remote',
        kind: 'remote',
        title: '远程任务',
        status: 'completed',
        device_name: '协作电脑',
        result_text: text,
        output: '{"truncated technical',
        output_truncated: true,
      }}
    />,
  );
  expect(screen.queryByText('最后一段仍然可见')).not.toBeInTheDocument();
  const details = view.container.querySelector('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  expect(screen.getByText('最后一段仍然可见')).toBeVisible();
  expect(view.container.querySelector('.remote-detail > .markdown')?.textContent).toContain(
    '最后一段仍然可见',
  );
});

it('preserves existing inline remote replies', () => {
  const view = render(
    <RemoteExecution
      entry={{
        id: 'legacy',
        kind: 'remote',
        title: '远程任务',
        output: JSON.stringify({ text: '之前的回复' }),
      }}
    />,
  );
  const details = view.container.querySelector('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  expect(screen.getByText('之前的回复')).toBeVisible();
});
