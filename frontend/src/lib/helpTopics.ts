export function helpTopicForPath(path: string) {
  if (path.startsWith('/eval/compare')) return 'three-denominators';
  if (path.startsWith('/eval/diagnostics')) return 'efficiency-scatter';
  if (/^\/eval\/(batches|reports|direct-lab|execution)/.test(path)) return 'security-and-auth';
  if (path.startsWith('/leaderboard')) return 'macro-avg';
  if (path === '/') return 'gain-vs-guardrail';
  return 'orthogonal-dimensions';
}
