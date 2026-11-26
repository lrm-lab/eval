import { useId, useRef, useState } from 'react';
import { HelpCircle } from 'lucide-react';
import { useDismissibleLayer } from '../../lib/dismissibleLayer';
import { DismissiblePanel } from './DismissiblePanel';
import { RowActionIcon } from './RowActionIcon';

export function MethodologyPopover({
  title,
  summary,
  anchor = '#orthogonal-dimensions',
}: {
  title: string;
  summary: string;
  anchor?: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const layer = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const dismiss = useDismissibleLayer(layer, open, () => setOpen(false), () => trigger.current?.focus({ preventScroll: true }));

  return (
    <span
      ref={layer}
      {...dismiss}
      className="relative inline-flex items-center"
    >
      <button
        type="button"
        ref={trigger}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        title={title}
        aria-label={`口径说明：${title}`}
        className="inline-flex items-center justify-center rounded-full p-0.5 text-[var(--text-muted)] hover:text-[var(--brand-primary)] cursor-pointer"
      >
        <HelpCircle className="w-3.5 h-3.5" />
      </button>
      <DismissiblePanel
        open={open}
        id={panelId}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className="lrm-glass absolute left-1/2 -translate-x-1/2 top-6 z-40 w-64 rounded-lg p-3 text-left text-xs font-normal normal-case"
      >
        <div className="font-semibold text-[var(--text-primary)] mb-1">{title}</div>
        <p className="m-0 text-[var(--text-secondary)] leading-relaxed">{summary}</p>
        <RowActionIcon
          variant="open"
          label="查看完整方法学文档"
          to={`/toolkit/methodology${anchor}`}
          className="mt-2"
        />
      </DismissiblePanel>
    </span>
  );
}
