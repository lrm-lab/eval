import { useRef, useState, type ComponentPropsWithoutRef, type ReactNode, type RefObject } from 'react';
import { useDismissibleLayer } from '../../lib/dismissibleLayer';

type DetailsProps = Omit<ComponentPropsWithoutRef<'details'>, 'children' | 'onToggle' | 'open'> & {
  detailsRef?: RefObject<HTMLDetailsElement | null>;
  summary: ReactNode;
  children: ReactNode | (() => ReactNode);
};

export function DismissibleDetails({ detailsRef, summary, children, ...props }: DetailsProps) {
  const localRef = useRef<HTMLDetailsElement>(null);
  const ref = detailsRef ?? localRef;
  const [open, setOpen] = useState(false);
  const [visited, setVisited] = useState(false);
  const dismiss = useDismissibleLayer(ref, open, () => {
    if (ref.current) ref.current.open = false;
  }, () => ref.current?.querySelector<HTMLElement>(':scope > summary')?.focus({ preventScroll: true }));

  return (
    <details
      {...props}
      {...dismiss}
      ref={ref}
      data-dismissible-details=""
      onToggle={(event) => {
        const expanded = event.currentTarget.open;
        setOpen(expanded);
        if (expanded) setVisited(true);
      }}
    >
      {summary}
      <div inert={!open}>
        {typeof children === 'function' ? (visited ? children() : null) : children}
      </div>
    </details>
  );
}
