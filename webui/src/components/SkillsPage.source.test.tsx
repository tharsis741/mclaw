import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { skillsApi } from '../api';
import type { SkillDetail } from '../types';
import { SkillsPage, sourceLabel } from './SkillsPage';

vi.mock('../api', () => ({
  skillsApi: { list: vi.fn(), detail: vi.fn() },
}));

const original = 'https://github.com/MiniMax-AI/skills/tree/main/skills/minimax-docx';
const skill: SkillDetail = {
  name: 'minimax-docx',
  description: '创建 Word 文档',
  source: { type: 'github', original, author: 'MiniMax', license: 'MIT' },
  enabled: true,
  version: 'version-1',
  content: '# Word',
  tree: { entries: [] },
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(skillsApi.list).mockResolvedValue({ skills: [skill] });
  vi.mocked(skillsApi.detail).mockResolvedValue(skill);
});

it.each(['aihot', 'minimax-docx', 'group/khazix-writer'])(
  'recognizes the legacy bundled identifier %s',
  (original) => expect(sourceLabel({ type: 'bundled', original })).toBe('内置'),
);

it('shows the original author and links to the upstream skill in the list and details', async () => {
  render(<SkillsPage onUse={vi.fn()} />);
  const link = await screen.findByRole('link', { name: 'MiniMax' });
  expect(link).toHaveAttribute('href', original);
  expect(link).toHaveAttribute('target', '_blank');
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(screen.queryByText('其他来源')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: /minimax-docx 创建 Word 文档/ }));
  const drawer = await screen.findByRole('dialog', { name: 'minimax-docx 技能详情' });
  await userEvent.click(within(drawer).getByText('来源与版本'));
  expect(within(drawer).getByRole('link', { name: original })).toHaveAttribute('href', original);
});

it('shows a repository when a GitHub source does not record its author', () => {
  expect(sourceLabel({ type: 'github', original })).toBe('MiniMax-AI/skills');
  expect(sourceLabel({ type: 'github', original }, true)).toBe(original);
  expect(
    sourceLabel({ type: 'skills_sh', original: 'https://skills.sh/example/skills/report' }),
  ).toBe('Skills.sh');
});

it.each(['javascript:alert(1)', 'file:///C:/skills/demo', 'data:text/html,test'])(
  'does not turn the non-web source %s into a link',
  async (original) => {
    vi.mocked(skillsApi.list).mockResolvedValue({
      skills: [{ ...skill, source: { type: 'local', original } }],
    });
    render(<SkillsPage onUse={vi.fn()} />);
    expect(await screen.findByText('本机')).toBeVisible();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  },
);
