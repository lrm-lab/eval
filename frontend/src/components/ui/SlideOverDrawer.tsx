import type { ReactNode } from 'react';
import { Maximize2, X } from 'lucide-react';
import { ModalOverlay } from './ModalOverlay';

/**
 * L2 Slide-Over Drawer (v2.2.1 01-1 §2.1.5 & 01-2 §2.4)
 * Slides in from right (240ms ease-out-expo) while preserving 100% of underlying page
 * filters, baseline, scroll position, and FloatingCompareDock.
 */
export function SlideOverDrawer({
  open,
  onClose,
  title,
  subtitle,
  fullPageHref,
  onOpenFullPage,
  wide = false,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  fullPageHref?: string;
  onOpenFullPage?: () => void;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <ModalOverlay
      open={open}
      onClose={onClose}
      label={title}
      className="flex justify-end"
      backdropClassName="bg-black/35 backdrop-blur-[2px]"
      panelClassName={`flex h-full w-[calc(100%-2.75rem)] flex-col border-l border-[var(--border-subtle)] bg-[var(--bg-surface)] shadow-2xl ${
        wide ? 'max-w-5xl' : 'max-w-2xl'
      }`}
      slideFromRight
    >
      <div className="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
        <div className="min-w-0">
          <h2 className="m-0 text-sm font-semibold text-[var(--text-primary)] truncate">{title}</h2>
          {subtitle && <p className="m-0 mt-0.5 text-xs text-[var(--text-secondary)] truncate">{subtitle}</p>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {(onOpenFullPage || fullPageHref) && (
            <button
              type="button"
              onClick={() => {
                if (onOpenFullPage) onOpenFullPage();
                else if (fullPageHref) window.location.assign(fullPageHref);
              }}
              className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
            >
              <Maximize2 className="w-3.5 h-3.5" />
              <span>展开全页</span>
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭抽屉"
            data-dialog-autofocus
            className="rounded-lg p-1.5 text-[var(--text-secondary)] hover:bg-[var(--bg-surface)] hover:text-[var(--text-primary)] cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-5">{children}</div>
    </ModalOverlay>
  );
}
