import { useEffect, useRef, useState } from 'react';

export function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    if (!ref.current) return;
    const el = ref.current;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

/** Screen position of an element's centre relative to the main pane (where hover cards live). */
export function anchorOf(target: Element) {
  const main = target.closest('.main');
  const r = target.getBoundingClientRect();
  const m = main?.getBoundingClientRect() ?? { left: 0, top: 0 };
  return { x: r.left + r.width / 2 - m.left, y: r.top + r.height / 2 - m.top };
}
