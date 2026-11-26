import { useSyncExternalStore } from 'react';
import { AnimatePresence, m, useIsPresent, type HTMLMotionProps } from 'motion/react';
import { useAppStore } from '../../lib/store';

type SurfaceProps = Omit<HTMLMotionProps<'div'>, 'initial' | 'animate' | 'exit'>;

const motionPreference = typeof window === 'undefined' ? null : window.matchMedia('(prefers-reduced-motion: reduce)');

function subscribeToMotionPreference(callback: () => void) {
  motionPreference?.addEventListener('change', callback);
  return () => motionPreference?.removeEventListener('change', callback);
}

function systemReducesMotion() {
  return motionPreference?.matches ?? false;
}

export function usePrefersReducedMotion() {
  const reducedBySystem = useSyncExternalStore(subscribeToMotionPreference, systemReducesMotion, () => false);
  const reducedByPreference = useAppStore((state) => state.reducedMotion);
  return reducedBySystem || reducedByPreference;
}

function PanelSurface({ style, inert, 'aria-hidden': ariaHidden, ...props }: SurfaceProps) {
  const present = useIsPresent();

  return (
    <m.div
      {...props}
      initial={false}
      animate={{ opacity: 1, scale: 1, transition: { duration: 0 } }}
      exit={{ opacity: 0, scale: 0.97, transition: { duration: 0.14, ease: 'easeIn' } }}
      inert={!present || inert}
      aria-hidden={present ? ariaHidden : true}
      style={present ? style : { ...style, pointerEvents: 'none' }}
    />
  );
}

export function DismissiblePanel({ open, ...props }: SurfaceProps & { open: boolean }) {
  const reducedMotion = usePrefersReducedMotion();

  if (!open && reducedMotion) return null;

  return (
    <AnimatePresence initial={false}>
      {open && <PanelSurface {...props} />}
    </AnimatePresence>
  );
}
