import React, { useEffect, useRef, useState } from 'react';
import { LazyMotion, MotionConfig, domAnimation, m } from 'motion/react';
import { useAppStore } from '../../lib/store';

export const EASE_OUT_EXPO: [number, number, number, number] = [0.16, 1, 0.3, 1];
export const SPRING_SNAPPY = { type: 'spring' as const, stiffness: 420, damping: 32, mass: 0.8 };

export function ReducedMotionProvider({ children }: { children: React.ReactNode }) {
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  useEffect(() => {
    document.documentElement.setAttribute('data-reduced-motion', reducedMotion ? 'true' : 'false');
  }, [reducedMotion]);

  return (
    <LazyMotion features={domAnimation} strict>
      <MotionConfig reducedMotion={reducedMotion ? 'always' : 'user'}>{children}</MotionConfig>
    </LazyMotion>
  );
}

export function PageTransition({
  children,
  className = '',
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const reduced = useAppStore((s) => s.reducedMotion);
  return (
    <m.div
      initial={reduced ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduced ? 0 : 0.22, ease: EASE_OUT_EXPO }}
      className={className}
    >
      {children}
    </m.div>
  );
}

export function StaggerContainer({
  children,
  className = '',
  skip = false,
}: {
  children: React.ReactNode;
  className?: string;
  skip?: boolean;
}) {
  const reduced = useAppStore((s) => s.reducedMotion);
  if (skip || reduced) {
    return <div className={className}>{children}</div>;
  }
  return (
    <m.div
      initial="hidden"
      animate="visible"
      variants={{
        hidden: {},
        visible: { transition: { staggerChildren: 0.06 } },
      }}
      className={className}
    >
      {children}
    </m.div>
  );
}

export function StaggerItem({
  children,
  className = '',
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const reduced = useAppStore((s) => s.reducedMotion);
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <m.div
      variants={{
        hidden: { opacity: 0, y: 12 },
        visible: { opacity: 1, y: 0, transition: { duration: 0.36, ease: EASE_OUT_EXPO } },
      }}
      className={className}
    >
      {children}
    </m.div>
  );
}

export function AnimatedNumber({
  value,
  durationMs = 400,
  decimals = 0,
  suffix = '',
  className = '',
}: {
  value: number;
  durationMs?: number;
  decimals?: number;
  suffix?: string;
  className?: string;
}) {
  const reduced = useAppStore((s) => s.reducedMotion);
  const [display, setDisplay] = useState<number>(reduced ? value : 0);
  const prevRef = useRef<number>(reduced ? value : 0);

  useEffect(() => {
    if (reduced || !Number.isFinite(value)) {
      setDisplay(value);
      prevRef.current = value;
      return;
    }
    const startVal = prevRef.current;
    const startTs = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const p = Math.min(1, (now - startTs) / durationMs);
      const eased = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
      setDisplay(startVal + (value - startVal) * eased);
      if (p < 1) {
        raf = requestAnimationFrame(tick);
      } else {
        prevRef.current = value;
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, durationMs, reduced]);

  const formatted = Number.isFinite(display)
    ? decimals > 0
      ? display.toFixed(decimals)
      : Math.round(display).toLocaleString()
    : '—';

  return (
    <span className={`tabular-nums ${className}`}>
      {formatted}
      {suffix}
    </span>
  );
}

export function SpotlightCard({
  children,
  className = '',
  onClick,
}: {
  children: React.ReactNode;
  className?: string;
  onClick?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useAppStore((s) => s.reducedMotion);
  const [pos, setPos] = useState<{ x: number; y: number; active: boolean }>({ x: 0, y: 0, active: false });

  const handleMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (reduced || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    setPos({ x: e.clientX - rect.left, y: e.clientY - rect.top, active: true });
  };

  return (
    <div
      ref={ref}
      onMouseMove={handleMove}
      onMouseLeave={() => setPos((p) => ({ ...p, active: false }))}
      onClick={onClick}
      className={`lrm-spotlight rounded-xl overflow-hidden ${onClick ? 'cursor-pointer hover:-translate-y-0.5' : ''} ${className}`}
    >
      {!reduced && pos.active && (
        <div
          className="pointer-events-none absolute inset-0 transition-opacity duration-150"
          style={{
            background: `radial-gradient(280px circle at ${pos.x}px ${pos.y}px, var(--hero-glow-1), transparent 70%)`,
          }}
        />
      )}
      <div className="relative z-10">{children}</div>
    </div>
  );
}

export interface SlidingTabOption {
  id: string;
  label: string;
  badge?: string | number;
  icon?: React.ReactNode;
}

export function SlidingTabs({
  layoutId,
  options,
  value,
  onChange,
  size = 'md',
  className = '',
}: {
  layoutId: string;
  options: SlidingTabOption[];
  value: string;
  onChange: (id: string) => void;
  size?: 'sm' | 'md';
  className?: string;
}) {
  const reduced = useAppStore((s) => s.reducedMotion);
  return (
    <div
      role="tablist"
      className={`inline-flex items-center gap-0.5 rounded-lg p-1 border border-[var(--border-subtle)] bg-[var(--bg-elevated)] ${className}`}
    >
      {options.map((opt) => {
        const active = opt.id === value;
        return (
          <button
            key={opt.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(opt.id)}
            className={`relative z-10 inline-flex items-center gap-1.5 rounded-md font-medium transition-colors cursor-pointer ${
              size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-sm'
            } ${active ? 'text-[var(--text-primary)]' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
          >
            {active &&
              (reduced ? (
                <span
                  className="absolute inset-0 -z-10 rounded-md bg-[var(--bg-surface)] border border-[var(--border-subtle)]"
                  style={{ boxShadow: 'var(--shadow-card)' }}
                />
              ) : (
                <m.span
                  layoutId={layoutId}
                  transition={SPRING_SNAPPY}
                  className="absolute inset-0 -z-10 rounded-md bg-[var(--bg-surface)] border border-[var(--border-subtle)]"
                  style={{ boxShadow: 'var(--shadow-card)' }}
                />
              ))}
            {opt.icon}
            <span>{opt.label}</span>
            {opt.badge !== undefined && (
              <span className="ml-0.5 rounded px-1.5 py-0.2 text-[11px] tabular-nums bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                {opt.badge}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function CollapsiblePanel({
  open,
  instant = false,
  children,
  className = '',
}: {
  open: boolean;
  instant?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  const reduced = useAppStore((s) => s.reducedMotion);
  return (
    <div
      className={`lrm-collapse-grid ${className}`}
      data-open={open ? 'true' : 'false'}
      data-instant={instant || reduced ? 'true' : 'false'}
    >
      <div>{children}</div>
    </div>
  );
}
