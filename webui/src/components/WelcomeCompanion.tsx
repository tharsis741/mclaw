import { useEffect, useId, useLayoutEffect, useRef } from 'react';
import { LETTERS, type CompanionLayout } from './companion-layouts';
import { createCompanionMotion } from './companion-motion';
import sprites from './companion-sprites.json';

function Sprite({ name }: { name: keyof typeof sprites }) {
  const item = sprites[name];
  return (
    <span className="letter-sprite">
      <img
        data-sprite={name}
        src={`/welcome-letters/${name}.png`}
        alt=""
        draggable={false}
        decoding="async"
        style={{
          width: `${(item.width / item.w) * 100}%`,
          height: `${(item.height / item.h) * 100}%`,
          left: `${(-item.x / item.w) * 100}%`,
          top: `${(-item.y / item.h) * 100}%`,
        }}
      />
    </span>
  );
}

export function WelcomeCompanion({
  initialLayout,
  active = true,
  quiet = false,
}: {
  initialLayout: CompanionLayout;
  active?: boolean;
  quiet?: boolean;
}) {
  const id = useId();
  const element = useRef<HTMLButtonElement>(null);
  const motion = useRef<ReturnType<typeof createCompanionMotion> | null>(null);
  const settings = useRef({ active, quiet });
  settings.current = { active, quiet };
  useLayoutEffect(() => {
    const controller = createCompanionMotion(element.current!, initialLayout, settings.current);
    motion.current = controller;
    return () => {
      controller.dispose();
      motion.current = null;
    };
  }, [initialLayout]);
  useEffect(() => {
    motion.current?.setQuiet(quiet);
    motion.current?.setActive(active);
  }, [active, quiet]);

  return (
    <button
      ref={element}
      type="button"
      className="welcome-companion"
      data-active={active}
      data-quiet={quiet}
      aria-label="和字母小伙伴打个招呼"
      aria-describedby={`${id}-instructions`}
      disabled={!active || quiet}
      onClick={(event) => {
        const letter = (event.target as Element).closest<HTMLElement>('[data-letter]');
        if (letter)
          motion.current?.tap(LETTERS.indexOf(letter.dataset.letter as (typeof LETTERS)[number]));
        else if (event.detail === 0) motion.current?.tap(4);
      }}
    >
      <span id={`${id}-instructions`} className="sr-only">
        点击字母，看看其他伙伴的反应；按回车或空格与 W 打招呼。
      </span>
      <span className="sr-only" data-announcement="" aria-live="polite" />
      <span className="letter-stage" aria-hidden="true">
        <span className="letter-door" data-door="">
          <Sprite name="doorway" />
          <span className="letter-door-leaf" data-door-leaf="">
            <Sprite name="door-leaf" />
          </span>
        </span>
        {LETTERS.map((letter) => (
          <span className="letter-piece" data-letter={letter} key={letter}>
            <Sprite name={`letter-${letter.toLowerCase()}` as keyof typeof sprites} />
          </span>
        ))}
      </span>
      <img className="letter-fallback" src="/mclaw-wordmark-fold.svg" alt="" aria-hidden="true" />
    </button>
  );
}
