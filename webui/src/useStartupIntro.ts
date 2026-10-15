import { useEffect, useState } from 'react';

/** Load the page behind the intro, then uncover it once both are ready. */
export function useStartupIntro(ready: boolean) {
  const [reduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [entranceFinished, setEntranceFinished] = useState(reduced);
  const [finished, setFinished] = useState(false);
  const leaving = ready && entranceFinished;
  useEffect(() => {
    if (reduced) return;
    const timer = setTimeout(() => setEntranceFinished(true), 1200);
    return () => clearTimeout(timer);
  }, [reduced]);
  useEffect(() => {
    if (!leaving || reduced) return;
    const timer = setTimeout(() => setFinished(true), 240);
    return () => clearTimeout(timer);
  }, [leaving, reduced]);
  return { visible: !ready || (!reduced && !finished), leaving };
}
