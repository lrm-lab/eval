import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { ArrowLeft, Info, Maximize, Minimize, Search, SlidersHorizontal, X, ZoomIn } from 'lucide-react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { apiFetch } from '../../lib/api';
import { useDismissibleDialog } from '../../lib/dismissibleDialog';
import { useDismissibleLayer } from '../../lib/dismissibleLayer';
import {
  cellSearchText, cellTitle, changeState, datasetName, findMapLock, zeroBaselineHistory, historyReason, layoutTreemap, mapAxes, referenceBadge, visibleSectors,
  type EvaluationMap, type HistoryItem, type MapAxis, type MapCell, type Rectangle, type Sector,
} from '../../lib/evaluationMap';
import { useAppStore } from '../../lib/store';
import { SlidingTabs } from '../motion/primitives';
import { DismissiblePanel } from './DismissiblePanel';
import { HeatmapDetails } from './HeatmapDetails';
import { HomeDatasetSwitcher } from './HomeDatasetSwitcher';
import { StateNoticeCard } from './Skeletons';
import './heatmap.css';

const action = 'inline-flex min-h-9 min-w-0 cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-sm font-semibold text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)]';
const toolbarAction = `${action} max-sm:min-h-11 max-sm:min-w-11`;
const dialogClass = 'rounded-t-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--text-primary)] shadow-xl';
const phoneQuery = '(max-width: 767px)';
const subscribeToPhone = (callback: () => void) => {
  const query = window.matchMedia(phoneQuery);
  query.addEventListener('change', callback);
  return () => query.removeEventListener('change', callback);
};
const getPhone = () => window.matchMedia(phoneQuery).matches;

function useToolbarPopover() {
  const id = useId();
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const dismiss = useDismissibleLayer(container, open, () => setOpen(false), () => trigger.current?.focus());

  return {
    id,
    open,
    containerProps: { ref: container, ...dismiss },
    triggerProps: {
      ref: trigger,
      'aria-expanded': open,
      'aria-controls': id,
      onClick: () => setOpen((visible) => !visible),
    },
  };
}

function ChangeLegend() {
  const help = useToolbarPopover();
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <ul aria-label="涨跌图例" className="m-0 flex list-none flex-wrap items-center gap-x-2 gap-y-1 p-0 text-xs font-semibold sm:gap-x-3">
        <li className="heatmap-change inline-flex items-center gap-1.5" data-tone="up">
          <span className="heatmap-scale" aria-hidden />↑ 上升
        </li>
        <li className="heatmap-change inline-flex items-center gap-1.5" data-tone="down">
          <span className="heatmap-scale" aria-hidden />↓ 下降
        </li>
        <li>= 持平</li>
        <li className="text-[var(--text-secondary)]">— 暂无分差</li>
      </ul>
      <div {...help.containerProps}>
        <button type="button" {...help.triggerProps} className="flex min-h-10 cursor-pointer items-center gap-1 rounded-md px-1.5 text-xs font-semibold text-[var(--text-secondary)] hover:bg-[var(--bg-elevated)] sm:min-h-8">
          <Info className="size-3.5 shrink-0" aria-hidden /> 读图
        </button>
        <DismissiblePanel open={help.open} id={help.id} className="heatmap-help-panel grid origin-top-right gap-2 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-3 text-xs leading-relaxed text-[var(--text-secondary)] shadow-lg">
          <p className="m-0">板块按登记题量分配面积，格子大小代表已发布评测次数。</p>
          <p className="m-0">涨跌幅越大，颜色越亮。首次出分或缺可比前次时，有效评分对照 0 分。</p>
          <p className="m-0">点开格子可查看评分、对照来源和暂无分差的原因。</p>
        </DismissiblePanel>
      </div>
    </div>
  );
}

function HeatmapFilters({ catalogue, locks, onSelect }: {
  catalogue?: EvaluationMap;
  locks: Record<MapAxis, string>;
  onSelect: (axis: MapAxis, value: string) => void;
}) {
  const layer = useToolbarPopover();
  const filters = mapAxes.map(({ id, label }) => ({
    id, label, options: catalogue?.groups[id] ?? [],
    active: catalogue ? findMapLock(catalogue, id, locks[id]) : undefined,
  }));

  return (
    <div className="md:relative" {...layer.containerProps}>
      <button type="button" aria-label="筛选" title="筛选" {...layer.triggerProps} className={`${toolbarAction} relative`}>
        <SlidersHorizontal className="size-4 shrink-0" aria-hidden /> <span className="hidden sm:inline">筛选</span>
        {filters.some(({ active }) => active) && (
          <span className="size-1.5 shrink-0 rounded-full bg-[var(--brand-primary)] max-sm:absolute max-sm:top-1.5 max-sm:right-1.5" title="已筛选" />
        )}
      </button>
      <DismissiblePanel open={layer.open} id={layer.id} className="absolute top-auto right-2 z-20 mt-2 grid w-[min(16rem,calc(100vw-2rem))] origin-top-right gap-2 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-3 shadow-lg md:top-full md:right-0">
        {filters.map(({ id, label, options, active }) => (
          <label key={id} className="grid min-w-0 grid-cols-[4.25rem_1fr] items-center text-xs font-semibold">
            <span>{label}</span>
            <select value={active?.id ?? ''} onChange={(event) => onSelect(id, event.target.value)}
              className="min-w-0 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-elevated)] py-1.5 text-sm">
              <option value="">全部</option>
              {active && !options.some((group) => group.id === active.id) && <option value={active.id}>{active.label}</option>}
              {[...options].sort((a, b) => a.label.localeCompare(b.label))
                .map((group) => <option key={group.id} value={group.id}>{group.label}</option>)}
            </select>
          </label>
        ))}
      </DismissiblePanel>
    </div>
  );
}

interface CellHistory {
  result?: HistoryItem;
  pending: boolean;
  failed: boolean;
  retry?: () => void;
}

type HistoryByCell = Map<string, CellHistory>;

function Tile({ rect, axis, cell, history, selected, onSelect }: {
  rect: Rectangle;
  axis: MapAxis;
  cell: MapCell;
  history?: CellHistory;
  selected: boolean;
  onSelect: () => void;
}) {
  const [primary, secondary] = cellTitle(cell, axis);
  const state = changeState(history?.result, history?.pending, history?.failed);
  const reference = referenceBadge(history?.result);
  const statusLabel = state.tone === 'unknown' && !history?.pending && !history?.failed
    ? historyReason(cell, history?.result) : state.label;
  const intensity = Math.sqrt(Math.min(100, Math.abs(history?.result?.delta_pp ?? 0))) * 10;
  const text = [datasetName(cell), primary, secondary, cell.dataset_version, cell.split, statusLabel, state.value, reference,
    `${cell.published_run_count} 次已发布`].filter(Boolean).join(' · ');
  const showTitle = rect.width >= 68 && rect.height >= 34;
  const showValue = rect.width >= 73 && rect.height >= (showTitle ? 55 : 20);
  return (
    <button
      id={`heatmap-tile-${cell.id}`}
      type="button"
      title={text}
      aria-label={text}
      aria-pressed={selected}
      data-tone={state.tone}
      data-runs={cell.published_run_count}
      onClick={onSelect}
      style={{
        left: rect.x, top: rect.y, width: rect.width, height: rect.height, '--heat-intensity': `${intensity}%`,
      } as CSSProperties}
      className="heatmap-tile absolute flex min-w-0 cursor-pointer flex-col justify-center overflow-hidden text-left"
    >
      {showTitle && <span className="w-full truncate px-2 text-xs leading-tight font-bold sm:text-sm">{primary}</span>}
      {showTitle && rect.height >= 72 && (
        <span className="w-full truncate px-2 text-[11px] leading-tight">{secondary}</span>
      )}
      {(showValue || rect.width >= 17 && rect.height >= 19) && (
        <span className={`block w-full truncate font-semibold tabular-nums ${showValue ? 'mt-0.5 px-2 text-[11px] sm:text-xs' : 'text-center text-[10px]'}`}>
          {state.symbol}{showValue && (state.value
            ? ` ${state.value}${reference ? ` · ${reference}` : ''}` : rect.width >= 125 ? ` ${statusLabel}` : null)}
        </span>
      )}
    </button>
  );
}

function SectorMap({ sector, rect, axis, zoomed, history, selected, onZoom, onSelect }: {
  sector: Sector;
  rect: Rectangle;
  axis: MapAxis;
  zoomed: boolean;
  history: HistoryByCell;
  selected?: string;
  onZoom: () => void;
  onSelect: (cell: MapCell) => void;
}) {
  const header = rect.width >= 64 && rect.height >= 44 ? 27 : 0;
  const hasRuns = sector.cells.some((cell) => cell.published_run_count > 0);
  const tiles = layoutTreemap(sector.cells, (cell) => cell.published_run_count, {
    x: 0, y: header, width: rect.width, height: rect.height - header,
  });
  const title = `${sector.label} · ${sector.registered_task_count == null ? '题量未登记' : `${sector.registered_task_count.toLocaleString()} 道登记题`}`;
  return (
    <section className="heatmap-sector" aria-label={title} data-sector={sector.id}
      data-registered-tasks={sector.registered_task_count ?? undefined}
      style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height }}>
      {header > 0 && (
        <button type="button" onClick={onZoom} title={zoomed ? '返回全图' : `放大 ${title}`}
          aria-label={zoomed ? '返回全图' : `放大 ${title}`}
          className="flex w-full min-w-0 cursor-pointer items-center gap-1.5 overflow-hidden bg-[var(--bg-surface)] px-2 text-left text-xs font-bold hover:bg-[var(--brand-tint)] focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-[var(--brand-primary)]"
          style={{ height: header }}>
          <span className="truncate">{sector.label}</span>
          {rect.width > 155 && (
            <span className="shrink-0 text-[10px] font-normal text-[var(--text-secondary)]">
              {sector.registered_task_count == null ? '题量未登记' : `${sector.registered_task_count.toLocaleString()} 题`}
            </span>
          )}
          {rect.width > 85 && (zoomed
            ? <Minimize className="ml-auto size-3.5 shrink-0 text-[var(--text-secondary)]" aria-hidden />
            : <ZoomIn className="ml-auto size-3.5 shrink-0 text-[var(--text-secondary)]" aria-hidden />)}
        </button>
      )}
      {tiles.map(({ item, ...position }) => (
        <Tile key={item.id} rect={position} axis={axis} cell={item} history={history.get(item.id)}
          selected={item.id === selected} onSelect={() => onSelect(item)} />
      ))}
      {!hasRuns && rect.width >= 115 && rect.height >= 65 && (
        <p className="mx-2 text-xs text-[var(--text-secondary)]">暂无已发布评测，可从搜索查看记录。</p>
      )}
    </section>
  );
}

function MapSearch({ sectors, axis, history, onSector, onCell, dialog, unweighted, session }: {
  sectors: Sector[];
  axis: MapAxis;
  history: HistoryByCell;
  onSector: (sector: Sector) => void;
  onCell: (sector: Sector, cell: MapCell) => void;
  dialog: ReturnType<typeof useDismissibleDialog>;
  unweighted: boolean;
  session: number;
}) {
  const titleId = useId();
  const [term, setTerm] = useState('');
  useLayoutEffect(() => setTerm(''), [session]);
  const terms = term.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const matches = sectors.flatMap((sector) => {
    const cells = sector.cells.filter((cell) => {
      if (unweighted && (sector.registered_task_count ?? 0) > 0 && cell.published_run_count > 0) return false;
      const text = `${sector.label.toLowerCase()} ${cellSearchText(cell)}`;
      return terms.every((word) => text.includes(word));
    });
    return cells.length ? [{ sector, cells }] : [];
  });

  function submit(sector: Sector, cell?: MapCell) {
    dialog.close();
    if (cell) onCell(sector, cell);
    else onSector(sector);
  }

  return (
    <dialog {...dialog.dialogProps} aria-labelledby={titleId}
      className={`fixed inset-x-0 bottom-0 top-auto m-0 w-full max-w-none origin-bottom overflow-hidden p-0 backdrop:bg-black/45 md:inset-0 md:m-auto md:max-w-xl md:origin-center md:rounded-2xl ${dialogClass}`}>
      <div className="flex max-h-[min(82dvh,42rem)] flex-col p-3 md:p-4">
        <div className="mb-2 flex items-center justify-between gap-3">
          <h3 id={titleId} className="m-0 text-base font-bold">{unweighted ? '查找未绘制记录' : '查找评测组合'}</h3>
          <button type="button" aria-label="关闭搜索" className={action} onClick={dialog.close}>
            <X className="size-4" aria-hidden />
          </button>
        </div>
        <form onSubmit={(event) => {
          event.preventDefault();
          if (terms.length && matches[0]?.cells[0]) submit(matches[0].sector, matches[0].cells[0]);
        }}>
          <input type="search" aria-label="搜索评测组合" autoFocus placeholder="搜索数据集、模型、Harness 或批次"
            value={term} onChange={(event) => setTerm(event.target.value)}
            className="mb-2 w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-elevated)] px-3 py-2 text-base focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)] md:text-sm" />
        </form>
        <p className="mb-2 mt-0 text-xs text-[var(--text-secondary)]" role="status">
          {matches.reduce((sum, match) => sum + match.cells.length, 0)} 个组合
        </p>
        <div className="min-h-0 overflow-auto overscroll-contain">
          {matches.map(({ sector, cells }) => (
            <section key={sector.id} aria-label={sector.label} className="mb-2">
              <button type="button" onClick={() => submit(sector)}
                className="flex min-h-9 w-full cursor-pointer items-center justify-between gap-2 rounded-lg bg-[var(--bg-elevated)] px-2.5 text-left text-sm font-semibold hover:bg-[var(--brand-tint)]">
                <span className="[overflow-wrap:anywhere]">{sector.label}</span><ZoomIn className="size-4 shrink-0" aria-hidden />
              </button>
              <ul className="m-0 list-none p-0">
                {cells.map((cell) => {
                  const [primary, secondary] = cellTitle(cell, axis);
                  const status = history.get(cell.id);
                  const change = changeState(status?.result,
                    status?.pending || (!status && Boolean(cell.latest_scored_batch)), status?.failed);
                  const statusLabel = change.tone === 'unknown' && !status?.pending && !status?.failed
                    && (!cell.latest_scored_batch || status?.result) ? historyReason(cell, status?.result) : change.label;
                  const reference = referenceBadge(status?.result);
                  return (
                    <li key={cell.id}>
                      <button type="button" onClick={() => submit(sector, cell)}
                        className="flex min-h-10 w-full cursor-pointer items-center justify-between gap-3 rounded-lg border-b border-[var(--border-subtle)] px-2.5 py-1.5 text-left text-sm hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-[var(--brand-primary)]">
                        <span className="min-w-0 [overflow-wrap:anywhere]">
                          <strong>{primary}</strong> · {secondary}
                          <span className="block text-xs text-[var(--text-secondary)]">
                            {[cell.dataset_version, cell.split, cell.checkpoint, cell.thinking_mode, cell.runner, cell.scoring_protocol].filter(Boolean).join(' · ')}
                          </span>
                        </span>
                        <span data-tone={change.tone} className="heatmap-change shrink-0 text-xs font-semibold tabular-nums">
                          {change.value ? `${change.symbol} ${change.value}${reference ? ` · ${reference}` : ''}` : statusLabel}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </dialog>
  );
}

type DetailPanelProps = {
  cell: MapCell;
  history: HistoryByCell;
  refreshCatalogue: () => void;
};

function DetailContent({ heading, cell, history, closeButton, onClose, refreshCatalogue }: DetailPanelProps & {
  heading: string;
  closeButton?: React.RefObject<HTMLButtonElement | null>;
  onClose: () => void;
}) {
  const status = history.get(cell.id);
  return (
    <div className="p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 id={heading} className="m-0 text-xs font-semibold tracking-wide text-[var(--text-secondary)]">评测详情</h3>
        <button ref={closeButton} type="button" aria-label="关闭评测详情" className={action} onClick={onClose}>
          <X className="size-4" aria-hidden />
        </button>
      </div>
      <HeatmapDetails cell={cell} history={status?.result} pending={status?.pending ?? false}
        failed={status?.failed ?? false} onRetry={() => {
          if (status?.result?.reason === 'BATCH_UNAVAILABLE' || status?.result?.reason === 'RELEASE_GENERATION_MISMATCH') refreshCatalogue();
          else status?.retry?.();
        }} />
    </div>
  );
}

function DesktopDetailPanel({ onClose, ...detail }: DetailPanelProps & { onClose: () => void }) {
  const heading = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeButton.current?.focus({ preventScroll: true });
  }, [detail.cell.id]);
  return (
    <aside className="heatmap-side bg-[var(--bg-surface)]" aria-labelledby={heading}>
      <DetailContent heading={heading} closeButton={closeButton} onClose={onClose} {...detail} />
    </aside>
  );
}

function MobileDetailPanel({ cell, selectionKey, searchButton, onDismiss, ...detail }: Omit<DetailPanelProps, 'cell'> & {
  cell?: MapCell;
  selectionKey: string;
  searchButton: React.RefObject<HTMLButtonElement | null>;
  onDismiss: () => void;
}) {
  const heading = useId();
  const { dialogProps, showModal, close } = useDismissibleDialog();
  const [lastCell, setLastCell] = useState(cell);
  const animationRun = useRef(0);
  const shownCell = cell ?? lastCell;

  // A second click on the same cell can produce a fresh URL entry during exit.
  useLayoutEffect(() => {
    if (!cell) {
      close();
      return;
    }
    setLastCell(cell);
    if (!dialogProps.ref.current?.open) {
      animationRun.current += 1;
      showModal(document.getElementById(`heatmap-tile-${cell.id}`) ?? searchButton.current ?? undefined);
    }
  }, [cell, close, dialogProps.ref, searchButton, selectionKey, showModal]);

  return (
    <dialog {...dialogProps} aria-labelledby={heading} className={`heatmap-dialog p-0 ${dialogClass}`}
      onClose={(event) => {
        const element = event.currentTarget;
        if (element.open) return;
        if (cell) onDismiss();
        const run = ++animationRun.current;
        const animations = element.getAnimations();
        if (animations.length === 0) setLastCell(undefined);
        else void Promise.allSettled(animations.map((animation) => animation.finished)).then(() => {
          if (animationRun.current === run) setLastCell(undefined);
        });
      }}>
      {shownCell && <DetailContent heading={heading} cell={shownCell} onClose={close} {...detail} />}
    </dialog>
  );
}

export function useHomeEvaluationMap() {
  const user = useAppStore((state) => state.user);
  const viewerKey = user ? [user.user_id, user.role, user.projects, user.scopes] : null;
  return useQuery({
    queryKey: ['home-evaluation-map', viewerKey],
    queryFn: ({ signal }) => apiFetch<EvaluationMap>('/api/v1/home/evaluation-map', { signal }),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}

export function HomeEvaluationHeatmap({ catalogue }: { catalogue: ReturnType<typeof useHomeEvaluationMap> }) {
  const [params, setParams] = useSearchParams();
  const { key: selectionKey } = useLocation();
  const [axis, locks] = useMemo(() => {
    const view = params.get('view');
    return [
      mapAxes.find(({ id }) => id === view)?.id ?? 'dataset',
      { dataset: params.get('lock_dataset') ?? '', model: params.get('lock_model') ?? '', harness: params.get('lock_harness') ?? '' },
    ] as const;
  }, [params]);
  const [unweightedSearch, setUnweightedSearch] = useState(false);
  const [searchSession, setSearchSession] = useState(0);
  const search = useDismissibleDialog();
  const searchButton = useRef<HTMLButtonElement>(null);
  const section = useRef<HTMLElement>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const [bounds, setBounds] = useState({ x: 0, y: 0, width: 0, height: 0 });
  const [fullscreen, setFullscreen] = useState(false);
  const [fallbackFullscreen, setFallbackFullscreen] = useState(false);
  const phone = useSyncExternalStore(subscribeToPhone, getPhone, () => false);
  const user = useAppStore((state) => state.user);
  const viewerKey = user ? [user.user_id, user.role, user.projects, user.scopes] : null;

  const sectors = useMemo(() => catalogue.data ? visibleSectors(catalogue.data, axis, locks) : [],
    [catalogue.data, axis, locks]);
  const zoomId = params.get('sector') || params.get('subject');
  const zoom = zoomId ? sectors.find((sector) => sector.id === zoomId || sector.label.toLowerCase() === zoomId.toLowerCase()) : undefined;
  const shown = zoom ? [zoom] : sectors;
  const selectedId = params.get('cell');
  const selected = shown.flatMap((sector) => sector.cells).find((cell) => cell.id === selectedId);
  const unweightedCount = sectors.reduce((sum, sector) => sum + sector.cells.filter((cell) => (
    !(sector.registered_task_count && sector.registered_task_count > 0) || cell.published_run_count === 0
  )).length, 0);
  const batches = useMemo(() => {
    type HistoryRequest = { id: string; cellId: string; batch_id: string; generation_id: string; mode: 'auto' | 'reviewed' };
    const chunks: HistoryRequest[][] = [];
    const visible = (zoom ? [zoom] : sectors.filter((sector) => (sector.registered_task_count ?? 0) > 0))
      .flatMap((sector) => sector.cells.filter((cell) => cell.published_run_count > 0));
    const needsDetail = selected && !visible.some((cell) => cell.id === selected.id) ? [selected] : [];
    for (const cell of [...needsDetail, ...visible]) {
      if (cell.kind !== 'trials' || zeroBaselineHistory(cell)) continue;
      for (const [batch, mode] of [[cell.latest_scored_batch, 'auto'], [cell.reviewed_batch, 'reviewed']] as const) {
        if (!batch) continue;
        let chunk = chunks.at(-1);
        if (!chunk || chunk.length === 18) {
          chunk = [];
          chunks.push(chunk);
        }
        chunk.push({ id: `${cell.id}:${mode}`, cellId: cell.id, batch_id: batch.batch_id, generation_id: batch.generation_id, mode });
      }
    }
    return chunks;
  }, [sectors, zoom, selected]);
  const histories = useQueries({
    queries: batches.map((items) => ({
      queryKey: ['home-evaluation-history', viewerKey, items],
      queryFn: ({ signal }: { signal: AbortSignal }) => apiFetch<{ items: HistoryItem[] }>('/api/v1/compare/history', {
        method: 'POST', body: JSON.stringify({ items: items.map(({ cellId, ...item }) => item) }), signal,
      }),
      staleTime: 30_000,
      refetchInterval: 60_000,
      retry: 1,
    })),
  });
  const history: HistoryByCell = new Map();
  const reviewedHistory: HistoryByCell = new Map();
  batches.forEach((group, index) => {
    const query = histories[index];
    const results = new Map(query.data?.items.map((item) => [item.id, item]));
    group.forEach(({ id, cellId, mode }) => {
      const target = mode === 'reviewed' ? reviewedHistory : history;
      target.set(cellId, {
        result: results.get(id), pending: query.isPending, failed: query.isError, retry: () => { void query.refetch(); },
      });
    });
  });
  reviewedHistory.forEach((entry, cellId) => {
    const previous = history.get(cellId);
    if (!previous || ((previous.result?.reason !== null || previous.result.reference_label) && entry.result?.reason === null)) {
      history.set(cellId, entry);
    }
  });
  for (const cell of catalogue.data?.cells ?? []) {
    const entry = history.get(cell.id);
    if (entry && !entry.result) continue;
    const result = zeroBaselineHistory(cell, entry?.result);
    if (result) history.set(cell.id, { result, pending: false, failed: false });
  }

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setBounds({ x: 0, y: 0, width, height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [Boolean(catalogue.data)]);

  useEffect(() => {
    function sync() { setFullscreen(document.fullscreenElement === section.current); }
    document.addEventListener('fullscreenchange', sync);
    return () => document.removeEventListener('fullscreenchange', sync);
  }, []);

  useEffect(() => {
    if (!fallbackFullscreen) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = previous; };
  }, [fallbackFullscreen]);

  function changeUrl(values: Record<string, string | null>) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      Object.entries(values).forEach(([key, value]) => {
        if (value) next.set(key, value);
        else next.delete(key);
      });
      return next;
    }, { preventScrollReset: true });
  }

  function setZoom(sector: Sector | undefined) {
    const next = sector?.id;
    changeUrl({ sector: next ?? null, subject: null, cell: sector?.cells.some(({ id }) => id === selectedId) ? selectedId : null });
  }

  function closeDetail() {
    changeUrl({ cell: null });
    requestAnimationFrame(() => {
      (document.getElementById(`heatmap-tile-${selectedId}`) ?? searchButton.current)?.focus({ preventScroll: true });
    });
  }

  function openSearch(trigger: HTMLElement, onlyUnweighted = false) {
    setUnweightedSearch(onlyUnweighted);
    setSearchSession((session) => session + 1);
    search.showModal(trigger);
    search.dialogProps.ref.current?.querySelector('input')?.focus({ preventScroll: true });
  }

  async function toggleFullscreen() {
    if (document.fullscreenElement === section.current) {
      await document.exitFullscreen().catch(() => undefined);
    } else if (fallbackFullscreen) {
      setFallbackFullscreen(false);
    } else {
      try {
        if (!section.current?.requestFullscreen) throw new Error('Fullscreen is unavailable');
        await section.current.requestFullscreen();
      } catch {
        setFallbackFullscreen(true);
      }
    }
  }

  const rectangles = layoutTreemap(shown, (sector) => zoom ? 1 : sector.registered_task_count ?? 0, bounds);
  const fullscreenLabel = fullscreen || fallbackFullscreen ? '退出全屏' : '全屏看图';

  return (
    <section ref={section} aria-labelledby="heatmap-title" data-fullscreen-fallback={fallbackFullscreen || undefined}
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || (event.target as HTMLElement).closest('dialog')) return;
        if (document.fullscreenElement === section.current) {
          event.preventDefault();
          void document.exitFullscreen().catch(() => undefined);
        } else if (fallbackFullscreen) {
          setFallbackFullscreen(false);
        } else if (selected && !phone) {
          closeDetail();
        }
      }}
      className="evaluation-heatmap relative min-w-0 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)]">
      <header className="relative flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-0.5 rounded-t-xl border-b border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2 py-1 sm:px-3 sm:py-1.5">
        <h1 id="heatmap-title" className="sr-only">评测态势</h1>
        <div className="flex max-w-full flex-wrap items-center gap-1">
          <div onKeyDown={(event) => {
            const index = mapAxes.findIndex(({ id }) => id === axis);
            const direction = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0;
            if (!direction && event.key !== 'Home' && event.key !== 'End') return;
            const next = direction ? (index + direction + mapAxes.length) % mapAxes.length : event.key === 'Home' ? 0 : mapAxes.length - 1;
            event.preventDefault();
            event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
            changeUrl({ view: mapAxes[next].id, sector: null, subject: null });
          }}>
            <SlidingTabs layoutId="home-heatmap-axis" value={axis} options={mapAxes.map((item) => ({ ...item }))}
              className="max-sm:p-0.5 max-sm:[&_[role=tab]]:min-h-10"
              onChange={(value) => changeUrl({ view: value, sector: null, subject: null })} size="sm" />
          </div>
          <button type="button" ref={searchButton} className={toolbarAction} aria-label="搜索评测组合" title="搜索评测组合"
            aria-haspopup="dialog" onClick={(event) => openSearch(event.currentTarget)}>
            <Search className="size-4 shrink-0" aria-hidden /> <span className="hidden sm:inline">搜索</span>
          </button>
          <button type="button" className={toolbarAction} aria-label={fullscreenLabel} title={fullscreenLabel}
            onClick={() => { void toggleFullscreen(); }}>
            {fullscreen || fallbackFullscreen ? <Minimize className="size-4" aria-hidden /> : <Maximize className="size-4" aria-hidden />}
            <span className="hidden sm:inline">{fullscreenLabel}</span>
          </button>
          <HeatmapFilters catalogue={catalogue.data} locks={locks}
            onSelect={(id, value) => changeUrl({ [`lock_${id}`]: value, sector: null, subject: null, cell: null })} />
        </div>
        <ChangeLegend />
      </header>

      {zoom && (
        <div className="flex min-w-0 flex-wrap items-center gap-2 border-b border-[var(--border-strong)] px-2.5 py-1 sm:px-4">
          <button type="button" className={action} onClick={() => setZoom(undefined)}>
            <ArrowLeft className="size-4 shrink-0" aria-hidden /> 返回全图
          </button>
          {axis === 'dataset' ? (
            <HomeDatasetSwitcher choices={sectors.map((sector) => ({ key: sector.id, label: sector.label }))}
              subject={zoom.id} label={zoom.label} pending={catalogue.isPending} failed={catalogue.isError}
              onRetry={() => { void catalogue.refetch(); }}
              onChange={(key) => setZoom(sectors.find((sector) => sector.id === key))} />
          ) : <span className="font-semibold [overflow-wrap:anywhere]">{zoom.label}</span>}
        </div>
      )}

      {catalogue.isPending ? <div role="status" className="heatmap-canvas flex items-center justify-center text-sm text-[var(--text-secondary)]">正在载入可见评测…</div>
        : catalogue.isError && !catalogue.data ? (
          <div className="p-4"><StateNoticeCard kind="error" title="评测态势加载失败" onRetry={() => catalogue.refetch()} /></div>
        ) : (
          <div className="heatmap-workspace">
            <div ref={canvas} className="heatmap-canvas" aria-label="评测态势图" role="group">
              {rectangles.map(({ item, ...rect }) => (
                <SectorMap key={item.id} rect={rect} sector={item} axis={axis} zoomed={Boolean(zoom)}
                  selected={selectedId ?? undefined} history={history} onZoom={() => setZoom(zoom ? undefined : item)}
                  onSelect={(cell) => changeUrl({ cell: cell.id })} />
              ))}
              {rectangles.length === 0 && bounds.width > 0 && (
                <div className="grid h-full place-content-center px-4 text-center text-sm text-[var(--text-secondary)]">
                  {sectors.length ? '当前板块缺少登记题量，可从搜索打开记录。' : '当前筛选下没有可见评测。'}
                </div>
              )}
            </div>
            {phone ? <MobileDetailPanel cell={selected} selectionKey={selectionKey} history={history} searchButton={searchButton}
              onDismiss={() => changeUrl({ cell: null })} refreshCatalogue={() => { void catalogue.refetch(); }} />
              : selected && <DesktopDetailPanel cell={selected} history={history} onClose={closeDetail}
                refreshCatalogue={() => { void catalogue.refetch(); }} />}
          </div>
        )}
      {unweightedCount > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-1 border-t border-[var(--border-subtle)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] sm:px-4">
          <span>未参与面积计算的记录</span>
          <button type="button" aria-haspopup="dialog" onClick={(event) => openSearch(event.currentTarget, true)}
            className="min-h-8 cursor-pointer font-semibold text-[var(--brand-primary)] hover:underline">{unweightedCount} 个组合，查看详情</button>
        </div>
      )}
      {selectedId && catalogue.data && !selected && (
        <div role="status" className="px-4 py-2 text-sm">
          当前没有可查看的选中组合。 <button type="button" className="cursor-pointer text-[var(--brand-primary)]" onClick={closeDetail}>清除选择</button>
        </div>
      )}

      <MapSearch dialog={search} sectors={sectors} axis={axis} history={history} session={searchSession}
        unweighted={unweightedSearch} onSector={setZoom}
        onCell={(sector, cell) => changeUrl({ sector: sector.id, subject: null, cell: cell.id })} />
    </section>
  );
}
