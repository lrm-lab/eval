import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { ArrowLeft, Maximize, Minimize, Search, SlidersHorizontal, X, ZoomIn } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { apiFetch } from '../../lib/api';
import {
  cellSearchText, cellTitle, changeState, datasetName, findMapLock, layoutTreemap, mapAxes, referenceBadge, visibleSectors,
  type EvaluationMap, type HistoryItem, type MapAxis, type MapCell, type Rectangle, type Sector,
} from '../../lib/evaluationMap';
import { useAppStore } from '../../lib/store';
import { SlidingTabs } from '../motion/primitives';
import { HeatmapDetails } from './HeatmapDetails';
import { HomeDatasetSwitcher } from './HomeDatasetSwitcher';
import { StateNoticeCard } from './Skeletons';
import './heatmap.css';

const action = 'inline-flex min-h-9 min-w-0 cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-sm font-semibold text-[var(--text-primary)] hover:bg-[var(--bg-elevated)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)]';
const dialogClass = 'rounded-t-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--text-primary)] shadow-xl';
const phoneQuery = '(max-width: 767px)';
const subscribeToPhone = (callback: () => void) => {
  const query = window.matchMedia(phoneQuery);
  query.addEventListener('change', callback);
  return () => query.removeEventListener('change', callback);
};
const getPhone = () => window.matchMedia(phoneQuery).matches;

function ChangeLegend() {
  return (
    <ul aria-label="涨跌图例" className="m-0 flex list-none flex-wrap items-center gap-x-3 gap-y-0.5 p-0 text-xs font-semibold">
      <li className="heatmap-change" data-tone="up">↑ 上涨</li>
      <li className="heatmap-change" data-tone="down">↓ 下跌</li>
      <li>= 持平</li>
      <li className="text-[var(--text-secondary)]">批次对照 / 运行快照</li>
      <li className="text-[var(--text-secondary)]">— 尚无可比前次</li>
      <li className="text-[var(--text-secondary)]">… 等待计算</li>
    </ul>
  );
}

interface CellHistory {
  result?: HistoryItem;
  pending: boolean;
  failed: boolean;
  retry: () => void;
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
  const intensity = Math.min(51, 21 + Math.sqrt(Math.abs(history?.result?.delta_pp ?? 0)) * 3.6);
  const text = [datasetName(cell), primary, secondary, cell.dataset_version, cell.split, state.label, state.value, reference,
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
            ? ` ${state.value}${reference ? ` · ${reference}` : ''}` : rect.width >= 125 ? ` ${state.label}` : null)}
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

function MapSearch({ sectors, axis, history, onSector, onCell, dialogRef, unweighted }: {
  sectors: Sector[];
  axis: MapAxis;
  history: HistoryByCell;
  onSector: (sector: Sector) => void;
  onCell: (sector: Sector, cell: MapCell) => void;
  dialogRef: React.RefObject<HTMLDialogElement | null>;
  unweighted: boolean;
}) {
  const titleId = useId();
  const [term, setTerm] = useState('');
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
    dialogRef.current?.close();
    if (cell) onCell(sector, cell);
    else onSector(sector);
  }

  return (
    <dialog ref={dialogRef} aria-labelledby={titleId} onClose={() => setTerm('')}
      onClick={(event) => { if (event.target === event.currentTarget) dialogRef.current?.close(); }}
      className={`fixed inset-x-0 bottom-0 top-auto m-0 w-full max-w-none overflow-hidden p-0 backdrop:bg-black/45 md:inset-0 md:m-auto md:max-w-xl md:rounded-2xl ${dialogClass}`}>
      <div className="flex max-h-[min(82dvh,42rem)] flex-col p-3 md:p-4">
        <div className="mb-2 flex items-center justify-between gap-3">
          <h3 id={titleId} className="m-0 text-base font-bold">{unweighted ? '查找未绘制记录' : '查找评测组合'}</h3>
          <button type="button" aria-label="关闭搜索" className={action} onClick={() => dialogRef.current?.close()}>
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
                          {change.value ? `${change.symbol} ${change.value}` : change.label}
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

function DetailPanel({ phone, cell, history, onClose, refreshCatalogue }: {
  phone: boolean;
  cell: MapCell;
  history: HistoryByCell;
  onClose: () => void;
  refreshCatalogue: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const heading = useId();
  useEffect(() => {
    const element = dialog.current;
    if (phone && element && !element.open) element.showModal();
    else close.current?.focus({ preventScroll: true });
    return () => { if (element?.open) element.close(); };
  }, [phone, cell.id]);

  const status = history.get(cell.id);
  const detail = (
    <div className="p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 id={heading} className="m-0 text-xs font-semibold tracking-wide text-[var(--text-secondary)]">评测详情</h3>
        <button ref={close} type="button" aria-label="关闭评测详情" className={action} onClick={onClose}>
          <X className="size-4" aria-hidden />
        </button>
      </div>
      <HeatmapDetails cell={cell} history={status?.result} pending={status?.pending ?? false}
        failed={status?.failed ?? false} onRetry={() => {
          if (status?.result?.reason === 'BATCH_UNAVAILABLE' || status?.result?.reason === 'RELEASE_GENERATION_MISMATCH') refreshCatalogue();
          else status?.retry();
        }} />
    </div>
  );
  if (phone) return (
    <dialog ref={dialog} aria-labelledby={heading} className={`heatmap-dialog p-0 ${dialogClass}`}
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      {detail}
    </dialog>
  );
  return <aside className="heatmap-side bg-[var(--bg-surface)]" aria-labelledby={heading}>{detail}</aside>;
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
  const [axis, locks] = useMemo(() => {
    const view = params.get('view');
    return [
      mapAxes.find(({ id }) => id === view)?.id ?? 'dataset',
      { dataset: params.get('lock_dataset') ?? '', model: params.get('lock_model') ?? '', harness: params.get('lock_harness') ?? '' },
    ] as const;
  }, [params]);
  const [unweightedSearch, setUnweightedSearch] = useState(false);
  const search = useRef<HTMLDialogElement>(null);
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
  const filters = mapAxes.map(({ id, label }) => ({
    id, label, options: catalogue.data?.groups[id] ?? [],
    active: catalogue.data ? findMapLock(catalogue.data, id, locks[id]) : undefined,
  }));
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
      if (cell.kind !== 'trials') continue;
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

  function openSearch(onlyUnweighted = false) {
    setUnweightedSearch(onlyUnweighted);
    search.current?.showModal();
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
  const cellIds = new Set(shown.flatMap((sector) => sector.cells.map((cell) => cell.id)));
  const published = shown.reduce((sum, sector) => sum + sector.published_run_count, 0);

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
      <div className="border-b border-[var(--border-strong)] px-2.5 py-2 sm:px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
            <h2 id="heatmap-title" className="m-0 text-lg leading-tight font-bold tracking-tight">评测热力图</h2>
            {catalogue.data && (
              <p className="m-0 text-xs tabular-nums text-[var(--text-secondary)]">{shown.length} 个板块 · {cellIds.size} 个组合 · {published} 次已发布</p>
            )}
          </div>
          <div className="flex max-w-full flex-wrap items-center gap-1.5">
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
                onChange={(value) => changeUrl({ view: value, sector: null, subject: null })} size="sm" />
            </div>
            <button type="button" ref={searchButton} className={action} aria-label="搜索热力图" aria-haspopup="dialog" onClick={() => openSearch()}>
              <Search className="size-4 shrink-0" aria-hidden /> 搜索
            </button>
            <button type="button" className={`${action} heatmap-fullscreen-button`}
              onClick={() => { void toggleFullscreen(); }}>
              {fullscreen || fallbackFullscreen ? <Minimize className="size-4" aria-hidden /> : <Maximize className="size-4" aria-hidden />}
              {fullscreen || fallbackFullscreen ? '退出全屏' : '全屏看图'}
            </button>
            <details className="md:relative">
              <summary className={`${action} list-none [&::-webkit-details-marker]:hidden`}>
                <SlidersHorizontal className="size-3.5 shrink-0" aria-hidden /> 筛选
                {filters.some(({ active }) => active) && (
                  <span className="size-1.5 shrink-0 rounded-full bg-[var(--brand-primary)]" title="已筛选" />
                )}
              </summary>
              <div className="absolute top-auto right-2 z-20 mt-2 md:top-full md:right-0 grid w-[min(16rem,calc(100vw-2rem))] gap-2 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-3 shadow-lg">
                {filters.map(({ id, label, options, active }) => (
                  <label key={id} className="grid min-w-0 grid-cols-[4.25rem_1fr] items-center text-xs font-semibold">
                    <span>{label}</span>
                    <select value={active?.id ?? ''}
                      onChange={(event) => changeUrl({ [`lock_${id}`]: event.target.value, sector: null, subject: null, cell: null })}
                      className="min-w-0 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-elevated)] py-1.5 text-sm">
                      <option value="">全部</option>
                      {active && !options.some((group) => group.id === active.id) && <option value={active.id}>{active.label}</option>}
                      {[...options].sort((a, b) => a.label.localeCompare(b.label))
                        .map((group) => <option key={group.id} value={group.id}>{group.label}</option>)}
                    </select>
                  </label>
                ))}
              </div>
            </details>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
          <p className="m-0 text-xs text-[var(--text-secondary)]">板块按登记题量 · 色块按已发布评测次数</p>
          <ChangeLegend />
        </div>
      </div>

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
          <div className="p-4"><StateNoticeCard kind="error" title="评测热力图加载失败" onRetry={() => catalogue.refetch()} /></div>
        ) : (
          <div className="heatmap-workspace">
            <div ref={canvas} className="heatmap-canvas" aria-label="评测矩形热力图" role="group">
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
            {selected && <DetailPanel phone={phone} cell={selected} history={history} onClose={closeDetail}
              refreshCatalogue={() => { void catalogue.refetch(); }} />}
          </div>
        )}
      {unweightedCount > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-1 border-t border-[var(--border-subtle)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] sm:px-4">
          <span>未参与面积计算的记录</span>
          <button type="button" aria-haspopup="dialog" onClick={() => openSearch(true)}
            className="min-h-8 cursor-pointer font-semibold text-[var(--brand-primary)] hover:underline">{unweightedCount} 个组合，查看详情</button>
        </div>
      )}
      {selectedId && catalogue.data && !selected && (
        <div role="status" className="px-4 py-2 text-sm">
          当前没有可查看的选中组合。 <button type="button" className="cursor-pointer text-[var(--brand-primary)]" onClick={closeDetail}>清除选择</button>
        </div>
      )}

      <MapSearch dialogRef={search} sectors={sectors} axis={axis} history={history}
        unweighted={unweightedSearch} onSector={setZoom}
        onCell={(sector, cell) => changeUrl({ sector: sector.id, subject: null, cell: cell.id })} />
    </section>
  );
}
