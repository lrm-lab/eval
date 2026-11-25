import React from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, FileText, Lock, ShieldCheck } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';

interface TaskDetailResp {
  dataset_release_id: string;
  task_id: string;
  task_dir: string | null;
  task_checksum: string | null;
  split: string | null;
  source: string | null;
  difficulty: string | null;
  category: string | null;
  language: string | null;
  tags: string[];
  resources: Record<string, unknown>;
  instruction: string | null;
  has_answer_access: boolean;
}

export function TaskDetailPage() {
  const { datasetReleaseId = '', taskId = '' } = useParams<{ datasetReleaseId: string; taskId: string }>();

  const q = useQuery({
    queryKey: ['dataset-task-detail', datasetReleaseId, taskId],
    queryFn: () =>
      apiFetch<TaskDetailResp>(
        `/api/v1/datasets/${encodeURIComponent(datasetReleaseId)}/tasks/${encodeURIComponent(taskId)}`
      ),
  });

  const data = q.data;

  return (
    <PageTransition className="max-w-5xl mx-auto px-5 py-6 space-y-5">
      <div className="flex items-center gap-3">
        <Link
          to="/datasets"
          className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] no-underline"
        >
          <ArrowLeft className="w-3.5 h-3.5" /> 返回 Datasets
        </Link>
        <div className="min-w-0">
          <h1 className="m-0 text-lg font-bold font-mono text-[var(--text-primary)] truncate">{taskId}</h1>
          <div className="text-xs font-mono text-[var(--text-muted)]">{datasetReleaseId}</div>
        </div>
      </div>

      {q.isLoading ? (
        <TableSkeleton rows={6} cols={2} />
      ) : q.isError ? (
        <StateNoticeCard kind="forbidden" title="题目详情不可访问" description={(q.error as Error).message} />
      ) : data ? (
        <>
          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <div>
              <div className="text-[var(--text-muted)]">Split</div>
              <div className="mt-0.5 font-semibold">{data.split || 'default'}</div>
            </div>
            <div>
              <div className="text-[var(--text-muted)]">类别 / 难度</div>
              <div className="mt-0.5 font-semibold">
                {data.category || '—'} / {data.difficulty || '—'}
              </div>
            </div>
            <div>
              <div className="text-[var(--text-muted)]">Checksum</div>
              <div className="mt-0.5 font-mono truncate">{data.task_checksum || '—'}</div>
            </div>
            <div>
              <div className="text-[var(--text-muted)]">答案访问权限</div>
              <div className="mt-0.5 inline-flex items-center gap-1">
                {data.has_answer_access ? (
                  <span className="text-[var(--sem-pass-fg)] inline-flex items-center gap-1">
                    <ShieldCheck className="w-3.5 h-3.5" /> dataset:answer_read
                  </span>
                ) : (
                  <span className="text-[var(--text-muted)] inline-flex items-center gap-1">
                    <Lock className="w-3.5 h-3.5" /> 答案已隐藏
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5">
            <h2 className="m-0 text-sm font-bold text-[var(--text-primary)] flex items-center gap-1.5 mb-3">
              <FileText className="w-4 h-4 text-[var(--brand-primary)]" /> 题面说明（Instruction）
            </h2>
            {data.instruction ? (
              <pre className="m-0 text-xs leading-relaxed whitespace-pre-wrap break-words font-sans text-[var(--text-primary)]">
                {data.instruction}
              </pre>
            ) : (
              <div className="text-xs text-[var(--text-muted)]">
                该题未提供公开 instruction.md 或来源尚未批准 public_task_metadata / holdout_task_body 证据类。
              </div>
            )}
          </div>
        </>
      ) : null}
    </PageTransition>
  );
}
