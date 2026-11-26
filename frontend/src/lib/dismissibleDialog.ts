import { useCallback, useRef, type KeyboardEvent, type MouseEvent, type PointerEvent, type SyntheticEvent } from 'react';
import './dismissibleDialog.css';

function isBackdropEvent(event: MouseEvent<HTMLDialogElement>) {
  const dialog = event.currentTarget;
  if (event.target !== dialog) return false;
  const bounds = dialog.getBoundingClientRect();
  return event.clientX < bounds.left || event.clientX >= bounds.right
    || event.clientY < bounds.top || event.clientY >= bounds.bottom;
}

export function useDismissibleDialog() {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const pressedBackdrop = useRef(false);

  const showModal = useCallback((trigger?: HTMLElement) => {
    const dialog = ref.current;
    if (!dialog || dialog.open) return;
    const active = dialog.ownerDocument.activeElement;
    opener.current = trigger ?? (active instanceof HTMLElement ? active : null);
    pressedBackdrop.current = false;
    dialog.inert = false;
    dialog.showModal();
  }, []);

  const close = useCallback(() => {
    const dialog = ref.current;
    if (!dialog?.open) return;
    const trigger = opener.current;
    opener.current = null;
    pressedBackdrop.current = false;
    dialog.close();
    // The browser releases modal focus immediately. The fading contents stay inert.
    dialog.inert = true;
    if (trigger?.isConnected && dialog.ownerDocument.activeElement !== trigger) {
      trigger.focus({ preventScroll: true });
    }
  }, []);

  return {
    showModal,
    close,
    dialogProps: {
      ref,
      'data-dismissible-dialog': '',
      onCancel(event: SyntheticEvent<HTMLDialogElement>) {
        event.preventDefault();
        close();
      },
      onKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
        if (event.key !== 'Escape' || event.defaultPrevented || event.nativeEvent.isComposing) return;
        event.preventDefault();
        event.stopPropagation();
        close();
      },
      onPointerDown(event: PointerEvent<HTMLDialogElement>) {
        pressedBackdrop.current = event.isPrimary && event.button === 0 && isBackdropEvent(event);
      },
      onPointerCancel() {
        pressedBackdrop.current = false;
      },
      onClick(event: MouseEvent<HTMLDialogElement>) {
        const dismiss = pressedBackdrop.current && isBackdropEvent(event);
        pressedBackdrop.current = false;
        if (dismiss) close();
      },
    },
  };
}
