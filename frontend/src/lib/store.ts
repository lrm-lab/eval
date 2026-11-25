import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { AuthUser, CompareSubjectRef } from '../types/api';

export type ThemeMode = 'light' | 'dark';
export type TableDensity = 'compact' | 'comfortable';
export type ColumnPreset = 'overview' | 'efficiency' | 'diagnostics' | 'custom';

export interface ToastMessage {
  id: string;
  tone: 'success' | 'info' | 'warning' | 'error';
  text: string;
}

export interface RecentViewItem {
  path: string;
  label: string;
  kind: string;
  visitedAt: number;
}

interface AppStoreState {
  user: AuthUser | null;
  setUser: (u: AuthUser | null) => void;

  theme: ThemeMode;
  setTheme: (t: ThemeMode) => void;
  toggleTheme: () => void;

  reducedMotion: boolean;

  sidebarCollapsed: boolean;
  setSidebarCollapsed: (v: boolean) => void;

  density: TableDensity;
  setDensity: (d: TableDensity) => void;

  columnPreset: ColumnPreset;
  setColumnPreset: (p: ColumnPreset) => void;
  customColumns: string[];
  setCustomColumns: (cols: string[]) => void;

  // Floating Compare Dock basket (1..8 subjects, first or marked is baseline)
  compareBasket: CompareSubjectRef[];
  baselineId: string | null;
  publicBaselineId: string;
  setPublicBaselineId: (id: string) => void;
  addToCompare: (item: CompareSubjectRef) => void;
  removeFromCompare: (id: string) => void;
  reorderCompare: (fromIdx: number, toIdx: number) => void;
  setBaselineId: (id: string) => void;
  clearCompare: () => void;

  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;

  recentViews: RecentViewItem[];
  pushRecentView: (item: Omit<RecentViewItem, 'visitedAt'>) => void;

  toasts: ToastMessage[];
  pushToast: (tone: ToastMessage['tone'], text: string) => void;
  dismissToast: (id: string) => void;
}

export const useAppStore = create<AppStoreState>()(
  persist(
    (set, get) => ({
      user: null,
      setUser: (user) =>
        set((state) => state.user?.user_id === user?.user_id ? { user } : { user, toasts: [] }),

      theme: 'dark',
      setTheme: (theme) => {
        document.documentElement.setAttribute('data-theme', theme);
        set({ theme });
      },
      toggleTheme: () => {
        const next: ThemeMode = get().theme === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        set({ theme: next });
      },

      reducedMotion: false,

      sidebarCollapsed: false,
      setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),

      density: 'compact',
      setDensity: (density) => set({ density }),

      columnPreset: 'overview',
      setColumnPreset: (columnPreset) => set({ columnPreset }),
      customColumns: ['comparative_macro_avg', 'self_coverage_avg', 'avg_duration_sec', 'tokens', 'runner'],
      setCustomColumns: (customColumns) => set({ customColumns }),

      compareBasket: [],
      baselineId: null,
      publicBaselineId: 'auto',
      setPublicBaselineId: (publicBaselineId) => set({ publicBaselineId }),
      addToCompare: (item) => {
        const cur = get().compareBasket;
        if (cur.some((x) => x.id === item.id)) {
          get().pushToast('info', `「${item.alias}」已在对比中`);
          return;
        }
        if (cur.length >= 8) {
          get().pushToast('warning', '对比最多支持 8 个对象');
          return;
        }
        const next = [...cur, item];
        set({
          compareBasket: next,
          baselineId: get().baselineId || item.id,
        });
        get().pushToast('success', `已加入对比：${item.alias}`);
      },
      removeFromCompare: (id) => {
        const next = get().compareBasket.filter((x) => x.id !== id);
        const curBase = get().baselineId;
        set({
          compareBasket: next,
          baselineId: curBase === id ? next[0]?.id || null : curBase,
        });
      },
      reorderCompare: (fromIdx, toIdx) => {
        const list = [...get().compareBasket];
        if (fromIdx < 0 || toIdx < 0 || fromIdx >= list.length || toIdx >= list.length) return;
        const [moved] = list.splice(fromIdx, 1);
        list.splice(toIdx, 0, moved);
        set({ compareBasket: list });
      },
      setBaselineId: (baselineId) => set({ baselineId }),
      clearCompare: () => set({ compareBasket: [], baselineId: null }),

      commandPaletteOpen: false,
      setCommandPaletteOpen: (commandPaletteOpen) => set({ commandPaletteOpen }),

      recentViews: [],
      pushRecentView: (item) => {
        const cur = get().recentViews.filter((x) => x.path !== item.path);
        set({ recentViews: [{ ...item, visitedAt: Date.now() }, ...cur].slice(0, 12) });
      },

      toasts: [],
      pushToast: (tone, text) => {
        const id = `t_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
        set((s) => ({ toasts: [...s.toasts.slice(-3), { id, tone, text }] }));
        window.setTimeout(() => {
          set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
        }, 3200);
      },
      dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
    }),
    {
      name: 'lrm-eval-prefs-v1',
      merge: (persisted, current) => ({
        ...current,
        ...(persisted as Partial<AppStoreState>),
        reducedMotion: current.reducedMotion,
      }),
      partialize: (s) => ({
        theme: s.theme,
        sidebarCollapsed: s.sidebarCollapsed,
        density: s.density,
        columnPreset: s.columnPreset,
        customColumns: s.customColumns,
        compareBasket: s.compareBasket,
        baselineId: s.baselineId,
        publicBaselineId: s.publicBaselineId,
        recentViews: s.recentViews,
      }),
    }
  )
);
