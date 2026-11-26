import React, { lazy, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { apiFetch, getCookieValue } from './lib/api';
import { useAppStore } from './lib/store';
import type { AuthUser } from './types/api';
import { ReducedMotionProvider } from './components/motion/primitives';
import { AppShell } from './components/layout/AppShell';
import { LazyContent } from './components/ui/LazyContent';
import { LoginPage } from './pages/LoginPage';
import { HomePage } from './pages/HomePage';

const LeaderboardPage = lazy(() => import('./pages/LeaderboardPage').then((module) => ({ default: module.LeaderboardPage })));
const ModelsPage = lazy(() => import('./pages/ModelsPage').then((module) => ({ default: module.ModelsPage })));
const HarnessesPage = lazy(() => import('./pages/HarnessesPage').then((module) => ({ default: module.HarnessesPage })));
const BenchmarksPage = lazy(() => import('./pages/BenchmarksPage').then((module) => ({ default: module.BenchmarksPage })));
const RecommendPage = lazy(() => import('./pages/RecommendPage').then((module) => ({ default: module.RecommendPage })));
const DatasetsPage = lazy(() => import('./pages/DatasetsPage').then((module) => ({ default: module.DatasetsPage })));
const TaskDetailPage = lazy(() => import('./pages/TaskDetailPage').then((module) => ({ default: module.TaskDetailPage })));
const ReportsPage = lazy(() => import('./pages/ReportsPage').then((module) => ({ default: module.ReportsPage })));
const BatchDetailPage = lazy(() => import('./pages/BatchDetailPage').then((module) => ({ default: module.BatchDetailPage })));
const TrialInspectorPage = lazy(() => import('./pages/TrialInspectorPage').then((module) => ({ default: module.TrialInspectorPage })));
const ComparePage = lazy(() => import('./pages/ComparePage').then((module) => ({ default: module.ComparePage })));
const DiagnosticsPage = lazy(() => import('./pages/DiagnosticsPage').then((module) => ({ default: module.DiagnosticsPage })));
const DirectLabPage = lazy(() => import('./pages/DirectLabPage').then((module) => ({ default: module.DirectLabPage })));
const ExecutionPage = lazy(() => import('./pages/ExecutionPage').then((module) => ({ default: module.ExecutionPage })));
const SyntheticStudioPage = lazy(() => import('./pages/SyntheticStudioPage').then((module) => ({ default: module.SyntheticStudioPage })));
const SelectionBoardPage = lazy(() => import('./pages/SelectionBoardPage').then((module) => ({ default: module.SelectionBoardPage })));
const TrainingOpdPage = lazy(() => import('./pages/TrainingOpdPage').then((module) => ({ default: module.TrainingOpdPage })));
const ToolkitVerifyPage = lazy(() => import('./pages/ToolkitVerifyPage').then((module) => ({ default: module.ToolkitVerifyPage })));
const ToolkitParamsPage = lazy(() => import('./pages/ToolkitParamsPage').then((module) => ({ default: module.ToolkitParamsPage })));
const ManualsPage = lazy(() => import('./pages/ManualsPage').then((module) => ({ default: module.ManualsPage })));
const MethodologyPage = lazy(() => import('./pages/MethodologyPage').then((module) => ({ default: module.MethodologyPage })));
const AdminHealthPage = lazy(() => import('./pages/AdminHealthPage').then((module) => ({ default: module.AdminHealthPage })));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 25_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function AuthGuard({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const user = useAppStore((s) => s.user);
  const setUser = useAppStore((s) => s.setUser);
  const hasCsrfCookie = Boolean(getCookieValue('lrm_eval_csrf'));
  const [checking, setChecking] = useState(!user && hasCsrfCookie);

  useEffect(() => {
    if (!user && !hasCsrfCookie) {
      setChecking(false);
      return;
    }
    let active = true;
    apiFetch<AuthUser | { user: AuthUser }>('/api/v1/auth/me', { skipAuthRedirect: true })
      .then((res) => {
        if (!active) return;
        const resolved = 'user' in res && res.user ? res.user : (res as AuthUser);
        setUser(resolved && resolved.user_id ? resolved : null);
      })
      .catch(() => {
        if (active) setUser(null);
      })
      .finally(() => {
        if (active) setChecking(false);
      });
    return () => {
      active = false;
    };
  }, [hasCsrfCookie, setUser]);

  if (checking) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[var(--bg-canvas)] text-xs text-[var(--text-secondary)]">
        正在验证会话权限...
      </div>
    );
  }

  if (!user) {
    const returnTo = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={{ pathname: '/login', search: `?returnTo=${returnTo}`, hash: location.hash }} replace />;
  }

  return (
    <AppShell>
      <LazyContent label="页面" resetKey={location.pathname}>
        {children}
      </LazyContent>
    </AppShell>
  );
}

function ThemeAttrSync() {
  const theme = useAppStore((s) => s.theme);
  const reducedMotion = useAppStore((s) => s.reducedMotion);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-reduced-motion', reducedMotion ? 'true' : 'false');
  }, [theme, reducedMotion]);

  return null;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeAttrSync />
      <ReducedMotionProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/*"
              element={
                <AuthGuard>
                  <Routes>
                    <Route path="/" element={<HomePage />} />
                    <Route path="/leaderboard" element={<LeaderboardPage />} />
                    <Route path="/leaderboard/:track" element={<LeaderboardPage />} />
                    <Route path="/models" element={<ModelsPage />} />
                    <Route path="/models/:modelCanonical" element={<ModelsPage />} />
                    <Route path="/harnesses" element={<HarnessesPage />} />
                    <Route path="/harnesses/:harnessName" element={<HarnessesPage />} />
                    <Route path="/benchmarks" element={<BenchmarksPage />} />
                    <Route path="/benchmarks/:benchmarkSlug" element={<BenchmarksPage />} />
                    <Route path="/recommend" element={<RecommendPage />} />
                    <Route path="/datasets" element={<DatasetsPage />} />
                    <Route path="/datasets/:datasetReleaseId/tasks/:taskId" element={<TaskDetailPage />} />
                    <Route path="/eval/reports" element={<ReportsPage />} />
                    <Route path="/eval/batches/:batchId" element={<BatchDetailPage />} />
                    <Route path="/eval/batches/:batchId/trials/:trialId" element={<TrialInspectorPage />} />
                    <Route path="/eval/compare" element={<ComparePage />} />
                    <Route path="/compare/s/:shortId" element={<ComparePage />} />
                    <Route path="/compare/1v1/:a/vs/:b" element={<ComparePage />} />
                    <Route path="/eval/diagnostics" element={<DiagnosticsPage />} />
                    <Route path="/eval/execution" element={<ExecutionPage />} />
                    <Route path="/eval/direct-lab" element={<DirectLabPage />} />
                    <Route path="/synth/datasets" element={<SyntheticStudioPage />} />
                    <Route path="/synth/selection" element={<SelectionBoardPage />} />
                    <Route path="/training/opd" element={<TrainingOpdPage />} />
                    <Route path="/toolkit/verify" element={<ToolkitVerifyPage />} />
                    <Route path="/toolkit/params" element={<ToolkitParamsPage />} />
                    <Route path="/toolkit/manuals" element={<ManualsPage />} />
                    <Route path="/toolkit/methodology" element={<MethodologyPage />} />
                    <Route path="/admin/health" element={<AdminHealthPage />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                  </Routes>
                </AuthGuard>
              }
            />
          </Routes>
        </BrowserRouter>
      </ReducedMotionProvider>
    </QueryClientProvider>
  );
}

export default App;
