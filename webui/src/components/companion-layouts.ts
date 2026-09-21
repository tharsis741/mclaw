export type Pose = {
  x: number;
  y: number;
  h: number;
  r: number;
  z: number;
  opacity: number;
  scale: number;
  ry: number;
};

export const LETTERS = ['M', 'C', 'L', 'A', 'W'] as const;
export const ENTRANCE_SECONDS = 3.35;
export const SCENE_WIDTH = 760;
export const SCENE_TOP = 65;
export const clamp = (value: number) => Math.max(0, Math.min(1, value));
export const ease = (value: number) => 1 - Math.pow(1 - value, 3);
const pose = (x: number, y: number, h: number, r = 0, z = 1): Pose => ({
  x,
  y,
  h,
  r,
  z,
  opacity: 1,
  scale: 1,
  ry: 0,
});

export const COMPANION_LAYOUTS = {
  portrait: {
    label: '合影',
    positions: [
      pose(277, 353, 210, -3, 1),
      pose(281, 365, 134, -7, 5),
      pose(378, 365, 140, 0, 6),
      pose(441, 348, 202, 5, 2),
      pose(486, 366, 125, 8, 7),
    ],
  },
  stack: {
    label: '叠靠',
    positions: [
      pose(442, 365, 144, 6, 2),
      pose(318, 339, 108, -12, 5),
      pose(281, 365, 161, 0, 6),
      pose(425, 254, 141, 12, 3),
      pose(313, 231, 117, -14, 4),
    ],
  },
  circle: {
    label: '围坐',
    positions: [
      pose(281, 310, 139, -5, 1),
      pose(278, 365, 126, -14, 5),
      pose(377, 279, 129, -3, 1),
      pose(482, 322, 143, 6, 2),
      pose(397, 367, 141, 15, 7),
    ],
  },
  cascade: {
    label: '错落',
    positions: [
      pose(238, 361, 152, -5, 2),
      pose(328, 341, 119, -7, 3),
      pose(405, 318, 129, 1, 4),
      pose(468, 280, 145, 9, 1),
      pose(527, 364, 131, 11, 5),
    ],
  },
  pairs: {
    label: '结伴',
    positions: [
      pose(276, 317, 154, -4, 1),
      pose(279, 368, 126, -11, 4),
      pose(388, 365, 134, -3, 5),
      pose(481, 307, 158, 8, 1),
      pose(493, 364, 122, 11, 6),
    ],
  },
  line: {
    label: '排排坐',
    positions: [
      pose(219, 359, 133, -4, 1),
      pose(301, 363, 110, -5, 2),
      pose(376, 361, 126, 1, 3),
      pose(455, 359, 141, 3, 4),
      pose(549, 363, 116, 6, 5),
    ],
  },
} satisfies Record<string, { label: string; positions: Pose[] }>;

export type CompanionLayout = keyof typeof COMPANION_LAYOUTS;
export const LAYOUT_NAMES = Object.keys(COMPANION_LAYOUTS) as CompanionLayout[];
export const positionsFor = (layout: CompanionLayout): Pose[] =>
  COMPANION_LAYOUTS[layout].positions.map((value) => ({ ...value }));

export function entrancePoses(time: number, layout: CompanionLayout): Pose[] {
  return positionsFor(layout).map((target, index, targets) => {
    const start = 0.44 + index * 0.27 + (index === 4 ? 0.12 : 0);
    const local = clamp((time - start) / (index === 4 ? 1.08 : 0.93));
    const move = ease(local);
    const arc = Math.sin(local * Math.PI);
    const roll = [-16, -275, 7, 28, 108][index] * Math.pow(1 - local, 2);
    const wobble =
      local < 1 ? Math.sin(local * Math.PI * (index === 4 ? 4 : 3)) * (1 - local) * 5 : 0;
    const notice =
      index < 4 && local === 1
        ? Math.sin(clamp((time - 2.13) / 0.56) * Math.PI) * (target.x < targets[4].x ? 3 : -3)
        : 0;
    return {
      ...target,
      x: 380 + (target.x - 380) * move,
      y: 242 + (target.y - 242) * move - arc * (index === 1 ? 5 : index === 4 ? 28 : 13),
      scale: 0.38 + move * 0.62,
      r: target.r + roll + wobble + notice,
      ry: [0, 0, 72, -60, 20][index] * (1 - move),
      opacity: local > 0 ? clamp(local / 0.18) : 0,
      z: local < 0.5 ? 12 - index : target.z,
    };
  });
}
