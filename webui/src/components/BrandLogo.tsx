export function BrandLogo({ compact = false }: { compact?: boolean }) {
  return (
    <img
      className={compact ? 'mini-mark' : 'brand-mark'}
      src="/mclaw-logo.png"
      width={compact ? 18 : 30}
      height={compact ? 18 : 30}
      alt=""
      aria-hidden="true"
    />
  );
}
