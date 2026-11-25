import React, { useState } from 'react';
import { HelpCircle } from 'lucide-react';
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

  return (
    <span
      className="relative inline-flex items-center"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-expanded={open}
        title={title}
        aria-label={`口径说明：${title}`}
        className="inline-flex items-center justify-center rounded-full p-0.5 text-[var(--text-muted)] hover:text-[var(--brand-primary)] cursor-pointer"
      >
        <HelpCircle className="w-3.5 h-3.5" />
      </button>
      {open && (
        <div
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
        </div>
      )}
    </span>
  );
}
