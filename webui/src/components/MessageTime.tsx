import { memo } from 'react';

/** Both current Unix seconds and imported ISO timestamps use the viewer's local time. */
export const MessageTime = memo(function MessageTime({ value }: { value?: number | string }) {
  if (value === undefined || value === null || value === '') return null;
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  if (!Number.isFinite(date.getTime())) return null;
  const label = date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const full = date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
    timeZoneName: 'short',
  });
  return (
    <time className="message-time" dateTime={date.toISOString()} title={full}>
      {label}
    </time>
  );
});
