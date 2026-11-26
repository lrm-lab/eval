import { useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, m, useIsPresent } from 'motion/react';
import { EASE_OUT_EXPO } from '../motion/primitives';
import { usePrefersReducedMotion } from './DismissiblePanel';

type ModalOverlayProps = {
  open: boolean;
  label: string;
  onClose: () => void;
  className: string;
  backdropClassName: string;
  panelClassName: string;
  slideFromRight?: boolean;
  children: ReactNode;
};

function focusableControls(dialog: HTMLElement) {
  return Array.from(dialog.querySelectorAll<HTMLElement>(
    'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]',
  )).filter((element) => element.tabIndex >= 0 && !element.closest('[inert]') && element.getClientRects().length > 0);
}

function ModalSurface({
  label, onClose, className, backdropClassName, panelClassName, slideFromRight, children,
}: Omit<ModalOverlayProps, 'open'>) {
  const present = useIsPresent();
  const reduced = usePrefersReducedMotion();
  const dialogRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!present || !dialog) return;
    const opener = document.activeElement;
    const initial = dialog.querySelector<HTMLElement>('[data-dialog-autofocus]') ?? focusableControls(dialog)[0] ?? dialog;
    initial.focus({ preventScroll: true });
    return () => {
      const active = document.activeElement;
      if ((active === document.body || dialog.contains(active))
        && opener instanceof HTMLElement && opener.isConnected && !dialog.contains(opener)) {
        opener.focus({ preventScroll: true });
      }
    };
  }, [present]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!present || event.defaultPrevented) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;

    const controls = focusableControls(event.currentTarget);
    const first = controls[0];
    const last = controls.at(-1);
    if (!first || !controls.includes(event.target as HTMLElement)
      || event.target === (event.shiftKey ? first : last)) {
      event.preventDefault();
      (event.shiftKey ? last ?? event.currentTarget : first ?? event.currentTarget).focus({ preventScroll: true });
    }
  };

  const closed = slideFromRight ? { x: '100%' } : { opacity: 0, scale: 0.97, y: -6 };
  const opened = slideFromRight ? { x: 0 } : { opacity: 1, scale: 1, y: 0 };
  const duration = slideFromRight ? 0.24 : 0.18;

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal={present || undefined}
      aria-hidden={!present || undefined}
      aria-label={label}
      inert={!present}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className={`fixed inset-0 z-50 overflow-hidden ${className}`}
      style={!present ? { pointerEvents: 'none', visibility: reduced ? 'hidden' : undefined } : undefined}
    >
      <m.div
        aria-hidden="true"
        initial={reduced ? false : { opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={reduced ? undefined : { opacity: 0 }}
        transition={{ duration: reduced ? 0 : 0.14 }}
        onClick={onClose}
        className={`absolute inset-0 ${backdropClassName}`}
      />
      <m.div
        initial={reduced ? false : closed}
        animate={opened}
        exit={reduced ? undefined : { ...closed, transition: { duration: 0.14, ease: 'easeIn' } }}
        transition={{ duration: reduced ? 0 : duration, ease: EASE_OUT_EXPO }}
        className={`relative z-10 ${panelClassName}`}
      >
        {children}
      </m.div>
    </div>
  );
}

export function ModalOverlay({ open, ...props }: ModalOverlayProps) {
  const reducedMotion = usePrefersReducedMotion();
  if (!open && reducedMotion) return null;

  return createPortal(
    <AnimatePresence>{open && <ModalSurface {...props} />}</AnimatePresence>,
    document.body,
  );
}
