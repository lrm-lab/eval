import type { MouseEventHandler } from 'react';
import { ArrowUpRight, ListTree, Microscope } from 'lucide-react';
import { Link, type To } from 'react-router-dom';
import { useAppStore } from '../../lib/store';

const variants = {
  microscope: { icon: Microscope, label: '打开 Trial 显微镜', color: 'from-indigo-600 to-violet-700' },
  details: { icon: ListTree, label: '查看详情', color: 'from-teal-600 to-cyan-700' },
  open: { icon: ArrowUpRight, label: '打开链接', color: 'from-blue-600 to-indigo-700' },
};

type RowActionIconProps = {
  variant: keyof typeof variants;
  label?: string;
  size?: 'sm' | 'md';
  className?: string;
} & (
  | { to: To; href?: never; onClick?: never }
  | { href: string; target?: '_blank' | '_self'; to?: never; onClick?: never }
  | { onClick: MouseEventHandler<HTMLButtonElement>; disabled?: boolean; to?: never; href?: never }
);

export function RowActionIcon(props: RowActionIconProps) {
  const reducedMotion = useAppStore((state) => state.reducedMotion);
  const { variant, size = 'sm', className = '' } = props;
  const { icon: Icon, color, label: defaultLabel } = variants[variant];
  const label = props.label ?? defaultLabel;
  const motion = reducedMotion ? '' : 'motion-safe:transition-transform motion-safe:hover:scale-110 motion-safe:group-hover/row-action:animate-[pulse_650ms_ease-in-out_1]';
  const attributes = {
    title: label,
    'aria-label': label,
    className: `inline-flex ${size === 'md' ? 'size-9' : 'size-8'} shrink-0 items-center justify-center rounded-lg bg-gradient-to-br ${color} align-middle text-white no-underline cursor-pointer hover:brightness-110 hover:shadow-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)] disabled:cursor-not-allowed disabled:opacity-45 ${motion} ${className}`,
  };
  const icon = <Icon aria-hidden="true" className={size === 'md' ? 'size-[18px]' : 'size-4'} />;

  if (props.to !== undefined) return <Link {...attributes} to={props.to}>{icon}</Link>;
  if (props.href !== undefined) {
    return <a {...attributes} href={props.href} target={props.target} rel={props.target === '_blank' ? 'noopener noreferrer' : undefined}>{icon}</a>;
  }
  return <button {...attributes} type="button" disabled={props.disabled} onClick={props.onClick}>{icon}</button>;
}
