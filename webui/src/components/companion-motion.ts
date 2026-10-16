import {
  ENTRANCE_SECONDS,
  LETTERS,
  SCENE_TOP,
  SCENE_WIDTH,
  clamp,
  ease,
  entrancePoses,
  positionsFor,
  type CompanionLayout,
  type Pose,
} from './companion-layouts';
import sprites from './companion-sprites.json';

type Settings = { active: boolean; quiet: boolean };
type Scene = 'idle' | 'arrival' | 'tap' | 'idle-gesture';

export function createCompanionMotion(
  root: HTMLButtonElement,
  layout: CompanionLayout,
  initial: Settings,
) {
  const pieces = Array.from(root.querySelectorAll<HTMLElement>('[data-letter]'));
  const images = Array.from(root.querySelectorAll<HTMLImageElement>('img[data-sprite]'));
  const door = root.querySelector<HTMLElement>('[data-door]')!;
  const leaf = root.querySelector<HTMLElement>('[data-door-leaf]')!;
  const announcement = root.querySelector<HTMLElement>('[data-announcement]')!;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const loaded = new Set(images.filter((image) => image.complete && image.naturalWidth > 0));
  let { active, quiet } = initial;
  let inView = true;
  let entered = false;
  let failed = false;
  let disposed = false;
  let frame = 0;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let scene: Scene = 'idle';
  let elapsed = 0;
  let previous = 0;
  let scale = 1;
  let subject = 4;
  let nextIdle = 0;
  let variation = 0;
  const ready = () => loaded.size === images.length;
  const visible = () => active && inView && !document.hidden && ready() && !failed && !disposed;
  const canMove = () => visible() && !quiet && !reduced.matches;

  function cancel() {
    cancelAnimationFrame(frame);
    clearTimeout(idleTimer);
    frame = 0;
    idleTimer = undefined;
    previous = 0;
  }

  function paint() {
    const poses = scene === 'arrival' ? entrancePoses(elapsed, layout) : positionsFor(layout);
    if (scene === 'tap' || scene === 'idle-gesture') {
      const progress = clamp(elapsed / 0.95);
      const wave = Math.sin(progress * Math.PI);
      poses.forEach((pose, index) => {
        if (index === subject) {
          const strength = scene === 'tap' ? 1 : 0.45;
          pose.y -= Math.sin(progress * Math.PI) * (variation % 2 ? 7 : 14) * strength;
          pose.r += Math.sin(progress * Math.PI * 2) * (variation % 2 ? 8 : 5) * strength;
        } else {
          pose.r += (poses[subject].x < pose.x ? -3 : 3) * wave;
        }
      });
    }
    poses.forEach((pose: Pose, index) => {
      const meta = sprites[`letter-${LETTERS[index].toLowerCase()}` as keyof typeof sprites];
      const height = pose.h * scale;
      const width = (height * meta.w) / meta.h;
      const piece = pieces[index];
      piece.style.width = `${width}px`;
      piece.style.height = `${height}px`;
      piece.style.opacity = `${pose.opacity}`;
      piece.style.zIndex = `${40 + pose.z}`;
      piece.style.transform = `translate(${pose.x * scale - width / 2}px, ${(pose.y - SCENE_TOP) * scale - height}px) rotate(${pose.r}deg)${pose.ry ? ` rotateY(${pose.ry}deg)` : ''}${pose.scale !== 1 ? ` scale(${pose.scale})` : ''}`;
      piece.dataset.attention = scene === 'tap' || scene === 'idle-gesture' ? LETTERS[subject] : '';
    });
    const opening =
      elapsed < 2.7 ? ease(clamp((elapsed - 0.1) / 0.5)) : 1 - ease(clamp((elapsed - 2.7) / 0.4));
    door.style.opacity =
      scene === 'arrival'
        ? `${elapsed < 0.2 ? elapsed / 0.2 : 1 - ease(clamp((elapsed - 3.05) / 0.2))}`
        : '0';
    leaf.style.transform = `rotateY(${-103 * opening}deg)`;
    root.dataset.scene = scene;
    root.dataset.animating = String(scene !== 'idle');
    root.dataset.ready = String(ready());
  }

  function measure() {
    scale = (root.getBoundingClientRect().width || root.clientWidth || 516.8) / SCENE_WIDTH;
    root.style.setProperty('--letter-scale', `${scale}`);
    paint();
  }

  function scheduleIdle() {
    clearTimeout(idleTimer);
    if (!canMove()) return;
    idleTimer = setTimeout(
      () => {
        idleTimer = undefined;
        if (!canMove()) return;
        subject = nextIdle++ % LETTERS.length;
        variation++;
        start('idle-gesture');
      },
      7500 + Math.random() * 3500,
    );
  }

  function settle() {
    cancel();
    scene = 'idle';
    elapsed = 0;
    paint();
  }

  function tick(now: number) {
    frame = 0;
    if (!canMove()) {
      settle();
      return;
    }
    elapsed += Math.min((now - previous) / 1000, 0.05);
    previous = now;
    if (elapsed >= (scene === 'arrival' ? ENTRANCE_SECONDS : 0.95)) {
      settle();
      scheduleIdle();
      return;
    }
    paint();
    frame = requestAnimationFrame(tick);
  }

  function start(next: Scene) {
    cancel();
    scene = next;
    elapsed = 0;
    previous = performance.now();
    paint();
    frame = requestAnimationFrame(tick);
  }

  function reconcile() {
    settle();
    if (!visible()) return;
    if (!entered) {
      entered = true;
      if (canMove()) {
        start('arrival');
        return;
      }
    }
    scheduleIdle();
  }

  function loadedImage(event: Event) {
    loaded.add(event.currentTarget as HTMLImageElement);
    if (ready()) reconcile();
  }

  function failedImage() {
    failed = true;
    root.dataset.assetError = 'true';
    settle();
  }

  const observer =
    typeof IntersectionObserver === 'undefined'
      ? null
      : new IntersectionObserver((entries) => {
          const next = entries[0]?.isIntersecting ?? false;
          if (next !== inView) {
            inView = next;
            reconcile();
          }
        });
  const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
  images.forEach((image) => {
    image.addEventListener('load', loadedImage);
    image.addEventListener('error', failedImage);
  });
  observer?.observe(root);
  resize?.observe(root);
  if (!resize) window.addEventListener('resize', measure);
  document.addEventListener('visibilitychange', reconcile);
  reduced.addEventListener('change', reconcile);
  root.dataset.layout = layout;
  delete root.dataset.assetError;
  measure();
  reconcile();
  if (images.some((image) => image.complete && image.naturalWidth === 0)) failedImage();

  return {
    setActive(value: boolean) {
      if (active !== value) {
        active = value;
        reconcile();
      }
    },
    setQuiet(value: boolean) {
      if (quiet !== value) {
        quiet = value;
        reconcile();
      }
    },
    tap(index: number) {
      if (!visible() || quiet || index < 0 || index >= LETTERS.length || scene === 'arrival')
        return;
      subject = index;
      variation++;
      announcement.textContent = `和 ${LETTERS[index]} 打个招呼`;
      if (reduced.matches) return;
      start('tap');
    },
    dispose() {
      disposed = true;
      cancel();
      observer?.disconnect();
      resize?.disconnect();
      window.removeEventListener('resize', measure);
      document.removeEventListener('visibilitychange', reconcile);
      reduced.removeEventListener('change', reconcile);
      images.forEach((image) => {
        image.removeEventListener('load', loadedImage);
        image.removeEventListener('error', failedImage);
      });
    },
  };
}
