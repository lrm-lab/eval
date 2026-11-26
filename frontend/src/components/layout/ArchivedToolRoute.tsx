import type { ReactNode } from 'react';
import { Archive, ArrowRight } from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';
import { archivedToolLabel, canMaintainArchivedTools } from '../../lib/archivedTools';
import { useAppStore } from '../../lib/store';

export function ArchivedToolNotice({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <section aria-labelledby="archived-tool-title" className="rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] p-4 sm:p-5">
      <div className="flex items-start gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
          <Archive aria-hidden="true" className="size-5" />
        </span>
        <div className="min-w-0">
          <h1 id="archived-tool-title" className="m-0 text-base font-bold sm:text-lg">{title}已归档</h1>
          <p className="mt-1 text-sm text-[var(--text-secondary)]">已发布的结果和试验证据可在评测报告中查看。</p>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <Link to="/eval/reports" className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-elevated)] px-3 text-sm font-semibold no-underline hover:bg-[var(--brand-tint)]">
          查看评测报告 <ArrowRight aria-hidden="true" className="size-4" />
        </Link>
        {children}
      </div>
    </section>
  );
}

export function ArchivedToolRoute({ children }: { children: ReactNode }) {
  const role = useAppStore((s) => s.user?.role);
  const { pathname } = useLocation();

  if (canMaintainArchivedTools(role)) return <>{children}</>;
  return (
    <div className="mx-auto max-w-3xl px-4 py-6 sm:px-6">
      <ArchivedToolNotice title={archivedToolLabel(pathname) ?? '此工具'} />
    </div>
  );
}
