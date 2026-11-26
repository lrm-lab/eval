import { useEffect, useRef, type FocusEvent, type KeyboardEvent, type RefObject } from 'react';

function isOpen<T extends HTMLElement>(open: boolean | ((element: T) => boolean), element: T) {
  return typeof open === 'boolean' ? open : open(element);
}

export function useDismissibleLayer<T extends HTMLElement>(
  layer: RefObject<T | null>, open: boolean | ((element: T) => boolean), close: () => void, focusTrigger: () => void,
  isInsideClick?: (event: MouseEvent) => boolean,
) {
  useEffect(() => {
    if (open === false) return;
    function onDocumentClick(event: MouseEvent) {
      const element = layer.current;
      if (element && isOpen(open, element) && !event.composedPath().includes(element) && !isInsideClick?.(event)) close();
    }
    document.addEventListener('click', onDocumentClick, true);
    return () => document.removeEventListener('click', onDocumentClick, true);
  }, [layer, open, close, isInsideClick]);

  return {
    onBlur(event: FocusEvent<T>) {
      if (isOpen(open, event.currentTarget) && event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) close();
    },
    onKeyDown(event: KeyboardEvent<T>) {
      if (event.key !== 'Escape' || event.defaultPrevented || !isOpen(open, event.currentTarget)) return;
      event.preventDefault();
      event.stopPropagation();
      close();
      focusTrigger();
    },
  };
}

export function useDisclosureLayer<T extends HTMLElement>(open: boolean, close: () => void) {
  const ref = useRef<T>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const handlers = useDismissibleLayer(ref, open, close, () => triggerRef.current?.focus({ preventScroll: true }));
  return { layerProps: { ref, ...handlers }, triggerRef };
}
