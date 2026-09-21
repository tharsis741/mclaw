import { useEffect, useState } from 'react';
import { chooseWelcome, readLastWelcome, rememberWelcome } from '../welcome-content';
import { WelcomeCompanion } from './WelcomeCompanion';

export function Welcome({
  onPrompt,
  active = true,
  quiet = false,
}: {
  onPrompt: (prompt: string) => void;
  active?: boolean;
  quiet?: boolean;
}) {
  const [selection] = useState(() => chooseWelcome(readLastWelcome()));
  useEffect(() => {
    if (active) rememberWelcome(selection);
  }, [selection, active]);

  return (
    <section className="welcome" aria-label="开始新会话">
      <WelcomeCompanion initialLayout={selection.layout} active={active} quiet={quiet} />
      <h1>今天，一起完成什么？</h1>
      <div className="suggestions" role="group" aria-label="试试这些任务">
        {selection.actions.map(({ id, title, text, icon: Icon }) => (
          <button type="button" key={id} onClick={() => onPrompt(text)}>
            <Icon size={16} strokeWidth={1.65} aria-hidden="true" />
            <span>{title}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
