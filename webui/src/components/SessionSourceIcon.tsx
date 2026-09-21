import { Clock3 } from 'lucide-react';
import { ChannelLogo } from './ChannelLogo';

export function SessionSourceIcon({ source, size = 18 }: { source?: string; size?: number }) {
  if (source === 'scheduler') {
    return (
      <span className="session-source-icon" role="img" aria-label="定时任务" title="定时任务">
        <Clock3 size={size} aria-hidden="true" />
      </span>
    );
  }
  return <ChannelLogo source={source} size={size} />;
}
