import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, BookOpen, Database, Layers, Search, X } from 'lucide-react';
import { apiFetch } from '../../lib/api';
import { useAppStore } from '../../lib/store';
import { ModalOverlay } from '../ui/ModalOverlay';

interface SearchItem {
  entity_type: string;
  entity_id: string;
  title: string;
  subtitle: string;
  project_id: string;
}

const QUICK_ROUTES: SearchItem[] = [
  { entity_type: 'page', entity_id: '/leaderboard', title: 'LLM Stats 综合主榜', subtitle: '4 视图切换 · ARC 效率前沿 · 列预设', project_id: 'default' },
  { entity_type: 'page', entity_id: '/eval/compare', title: '动态自由对比工作台', subtitle: 'Gain vs Guardrail 三线结论 · 双栏 Diff · 导出', project_id: 'default' },
  { entity_type: 'page', entity_id: '/datasets', title: 'Datasets 三线能力树与题库浏览', subtitle: '14 项核心基准 · 零代码 taxonomy 驱动', project_id: 'default' },
  { entity_type: 'page', entity_id: '/eval/reports', title: '评测报告中心', subtitle: 'Harbor / VLMEvalKit / Direct 全量批次', project_id: 'default' },
  { entity_type: 'page', entity_id: '/toolkit/methodology', title: '评测方法学与口径说明', subtitle: '4 维正交 · Macro Avg · 交集分母 · Token 语义', project_id: 'default' },
];

export function CommandPalette() {
  const open = useAppStore((s) => s.commandPaletteOpen);
  const setOpen = useAppStore((s) => s.setCommandPaletteOpen);
  const navigate = useNavigate();

  const [q, setQ] = useState('');
  const [results, setResults] = useState<SearchItem[]>(QUICK_ROUTES);
  const [activeIdx, setActiveIdx] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen(!open);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, setOpen]);

  useEffect(() => {
    if (!open) return;
    if (!q.trim()) {
      setResults(QUICK_ROUTES);
      setActiveIdx(0);
      return;
    }
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const res = await apiFetch<{ items: SearchItem[] }>(`/api/v1/search?q=${encodeURIComponent(q.trim())}&limit=20`);
        setResults(res.items.length > 0 ? res.items : []);
        setActiveIdx(0);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 150);
    return () => window.clearTimeout(timer);
  }, [q, open]);

  const goItem = (item: SearchItem) => {
    setOpen(false);
    setQ('');
    if (item.entity_type === 'page') navigate(item.entity_id);
    else if (item.entity_type === 'batch') navigate(`/eval/batches/${encodeURIComponent(item.entity_id)}`);
    else if (item.entity_type === 'benchmark') navigate(`/benchmarks/${encodeURIComponent(item.entity_id)}`);
    else if (item.entity_type === 'model') navigate(`/models/${encodeURIComponent(item.entity_id)}`);
    else navigate('/leaderboard');
  };

  const onInputKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIdx((i) => Math.min(results.length - 1, i + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIdx((i) => Math.max(0, i - 1));
    } else if (e.key === 'Enter' && results[activeIdx]) {
      e.preventDefault();
      goItem(results[activeIdx]);
    }
  };

  return (
    <ModalOverlay
      open={open}
      onClose={() => setOpen(false)}
      label="全局快捷检索"
      className="flex items-start justify-center px-4 pt-[12vh]"
      backdropClassName="bg-black/40 backdrop-blur-xs"
      panelClassName="lrm-glass w-full max-w-xl overflow-hidden rounded-2xl"
    >
      <div className="flex items-center gap-2.5 px-4 py-3 border-b border-[var(--border-subtle)]">
        <Search className="w-4 h-4 text-[var(--text-muted)] shrink-0" />
        <input
          data-dialog-autofocus
          aria-label="检索模型、批次和基准"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onInputKey}
          placeholder="搜索模型、Harness、Benchmark、批次名（FTS5 实时过滤权限）..."
          className="flex-1 bg-transparent text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none"
        />
        <button
          type="button"
          onClick={() => setOpen(false)}
          aria-label="关闭全局快捷检索"
          className="rounded p-1 text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="max-h-80 overflow-y-auto p-2">
        {loading ? (
          <div className="p-4 text-center text-xs text-[var(--text-muted)]">检索中...</div>
        ) : results.length === 0 ? (
          <div className="p-6 text-center text-xs text-[var(--text-muted)]">未匹配到有权查看的记录</div>
        ) : (
          results.map((item, idx) => {
            const active = idx === activeIdx;
            const Icon =
              item.entity_type === 'benchmark'
                ? Database
                : item.entity_type === 'batch'
                  ? Layers
                  : BookOpen;
            return (
              <button
                key={`${item.entity_type}:${item.entity_id}`}
                type="button"
                onMouseEnter={() => setActiveIdx(idx)}
                onClick={() => goItem(item)}
                className={`w-full flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-left transition-colors cursor-pointer ${
                  active ? 'bg-[var(--brand-tint)] text-[var(--text-primary)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)]'
                }`}
              >
                <div className="flex items-center gap-2.5 min-w-0">
                  <Icon className="w-4 h-4 shrink-0 text-[var(--brand-primary)]" />
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-[var(--text-primary)] truncate">{item.title}</div>
                    <div className="text-[11px] text-[var(--text-muted)] truncate">{item.subtitle}</div>
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <span className="rounded px-1.5 py-0.5 text-[10px] uppercase bg-[var(--bg-elevated)] text-[var(--text-muted)]">
                    {item.entity_type}
                  </span>
                  <ArrowRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />
                </div>
              </button>
            );
          })
        )}
      </div>

      <div className="flex items-center justify-between px-4 py-2 border-t border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-[11px] text-[var(--text-muted)]">
        <span>↑↓ 选择 · Enter 跳转 · Esc 关闭</span>
        <span>SQLite FTS5 · 按项目与 Scope 鉴权</span>
      </div>
    </ModalOverlay>
  );
}
