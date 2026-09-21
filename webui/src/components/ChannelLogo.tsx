export function ChannelLogo({ source, size = 18 }: { source?: string; size?: number }) {
  if (source !== 'weixin' && source !== 'dingtalk') return null;
  return (
    <img
      className="channel-logo"
      src={`/channel-logos/${source === 'weixin' ? 'wechat' : 'dingtalk'}.svg`}
      width={size}
      height={size}
      alt={source === 'weixin' ? '微信' : '钉钉'}
    />
  );
}
