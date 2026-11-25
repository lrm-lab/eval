// 10-color colorblind-friendly deterministic palette (v2.2.1 01-1 §1.3)
export const CHART_PALETTE = [
  '#2563EB',
  '#10B981',
  '#F59E0B',
  '#EF4444',
  '#8B5CF6',
  '#06B6D4',
  '#EC4899',
  '#84CC16',
  '#F97316',
  '#6366F1',
] as const;

export function stableColorForKey(key: string): string {
  let hash = 2166136261;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return CHART_PALETTE[Math.abs(hash) % CHART_PALETTE.length];
}

export function fmtPct(val: number | null | undefined, digits = 2): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return '—';
  return `${val.toFixed(digits)}%`;
}

export function fmtScore(val: number | null | undefined, digits = 2): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return '—';
  return val.toFixed(digits);
}

export function fmtDeltaPP(val: number | null | undefined): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return '—';
  const sign = val > 0 ? '+' : '';
  return `${sign}${val.toFixed(2)} pp`;
}

export function fmtRelPct(val: number | null | undefined): string {
  if (val === null || val === undefined || !Number.isFinite(val)) return '—';
  const sign = val > 0 ? '+' : '';
  return `${sign}${val.toFixed(2)}%`;
}

export function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return '—';
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}m ${s.toFixed(0)}s`;
}

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(Math.round(n));
}

export function fmtDateShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  return iso.replace('T', ' ').slice(0, 16);
}

export function runScopeLabel(scope: string | null | undefined): string {
  switch (scope) {
    case 'full_split':
      return 'Formal Full';
    case 'subset':
      return 'Subset';
    case 'smoke':
      return 'Smoke';
    case 'reference':
      return 'Reference';
    default:
      return scope || 'unknown';
  }
}

export function escapeHtml(value: unknown): string {
  const s = value === null || value === undefined ? '' : String(value);
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function trackDisplayName(slug: string): string {
  switch (slug) {
    case 'agentic':
      return 'Agentic (提分主线)';
    case 'llm-text':
      return 'LLM — Text (护栏)';
    case 'vision-image':
      return 'Vision — Image (护栏)';
    case 'synthetic':
      return 'Synthetic (合成)';
    case 'extended':
      return 'Extended (历史扩展)';
    default:
      return slug;
  }
}
