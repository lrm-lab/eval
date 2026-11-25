import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BookText, FileText, Search } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface ManualSummary {
  doc_id: string;
  title: string;
  category: string;
  size_bytes: number;
  heading_count: number;
  snippet: string | null;
}

interface ManualDetail {
  doc_id: string;
  title: string;
  category: string;
  toc: Array<{ level: number; title: string; anchor: string }>;
  content: string;
}

export function ManualsPage() {
  const [q, setQ] = useState('');
  const [selectedId, setSelectedId] = useState('framework-readme');

  const listQ = useQuery({
    queryKey: ['toolkit-manuals', q],
    queryFn: () => apiFetch<{ items: ManualSummary[] }>(`/api/v1/toolkit/manuals${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ''}`),
  });

  useEffect(() => {
    const items = listQ.data?.items || [];
    if (items.length > 0 && !items.some((i) => i.doc_id === selectedId)) {
      setSelectedId(items[0].doc_id);
    }
  }, [listQ.data, selectedId]);

  const docQ = useQuery({
    queryKey: ['toolkit-manual-detail', selectedId],
    enabled: Boolean(selectedId),
    queryFn: () => apiFetch<ManualDetail>(`/api/v1/toolkit/manuals/${encodeURIComponent(selectedId)}`),
  });

  return (
    <PageTransition className="max-w-7xl mx-auto px-5 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="m-0 text-xl font-bold text-[var(--text-primary)] flex items-center gap-2">
            <BookText className="w-5 h-5 text-[var(--brand-primary)]" />
            <span>Manuals 框架与平台手册中心</span>
          </h1>
          <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
            实时只读挂载 197 framework/README.md、SETUP.md、DATASET_FORMAT.md、VLMEVALKIT.md 与 datasets/README.md（全路径脱敏）
          </p>
        </div>

        <div className="relative">
          <Search className="w-3.5 h-3.5 text-[var(--text-muted)] absolute left-2.5 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="全文检索手册..."
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] pl-8 pr-3 py-1.5 text-xs outline-none focus:border-[var(--brand-primary)]"
          />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-start">
        {/* Left catalog */}
        <div className="lg:col-span-3 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-2 space-y-1">
          {listQ.isLoading ? (
            <TableSkeleton rows={5} cols={1} />
          ) : (listQ.data?.items || []).length === 0 ? (
            <StateNoticeCard kind="empty" title="无匹配手册" />
          ) : (
            listQ.data!.items.map((item) => (
              <button
                key={item.doc_id}
                type="button"
                onClick={() => setSelectedId(item.doc_id)}
                className={`w-full text-left rounded-lg px-3 py-2 text-xs transition-colors cursor-pointer ${
                  selectedId === item.doc_id
                    ? 'bg-[var(--brand-tint)] text-[var(--brand-primary)] font-semibold'
                    : 'text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)]'
                }`}
              >
                <div className="flex items-center gap-1.5">
                  <FileText className="w-3.5 h-3.5 shrink-0" />
                  <span className="truncate">{item.title}</span>
                </div>
                {item.snippet && <div className="mt-1 text-[10px] text-[var(--text-muted)] line-clamp-2">{item.snippet}</div>}
              </button>
            ))
          )}
        </div>

        {/* Center document content */}
        <div className="lg:col-span-7 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5 text-xs">
          {docQ.isLoading ? (
            <TableSkeleton rows={8} cols={1} />
          ) : docQ.data ? (
            <article className="space-y-3">
              <h2 className="m-0 text-base font-bold text-[var(--text-primary)] pb-2 border-b border-[var(--border-subtle)]">
                {docQ.data.title}
              </h2>
              <pre className="m-0 whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-[var(--text-primary)]">
                {docQ.data.content}
              </pre>
            </article>
          ) : (
            <StateNoticeCard kind="empty" title="请选择左侧文档" />
          )}
        </div>

        {/* Right sticky On this page TOC */}
        <div className="lg:col-span-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3 text-xs lg:sticky lg:top-20">
          <div className="font-bold text-[11px] text-[var(--text-muted)] uppercase mb-2">On this page</div>
          <div className="space-y-1 max-h-[65vh] overflow-y-auto">
            {(docQ.data?.toc || []).map((h, i) => (
              <div
                key={`${h.anchor}-${i}`}
                style={{ paddingLeft: `${Math.max(0, h.level - 1) * 8}px` }}
                className="text-[11px] text-[var(--text-secondary)] truncate"
              >
                {h.title}
              </div>
            ))}
          </div>
        </div>
      </div>
    </PageTransition>
  );
}
