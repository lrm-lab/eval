import type { AuthUser } from '../types/api';

const toolLabels = new Map([
  ['/eval/execution', '受控评测执行'],
  ['/eval/direct-lab', 'Direct 原生验证台'],
  ['/toolkit/verify', '离线校验与算分'],
  ['/toolkit/params', '运行参数对照台账'],
  ['/synth/datasets', '合成题库工坊'],
  ['/synth/selection', 'RL/SFT 四通道选题'],
  ['/training/opd', '训练与 OPD 监控'],
]);

export function archivedToolLabel(path: string) {
  return toolLabels.get(path.split(/[?#]/, 1)[0].replace(/\/+$/, '').toLowerCase());
}

export function canMaintainArchivedTools(role?: AuthUser['role']): boolean {
  return role === 'researcher' || role === 'admin';
}
