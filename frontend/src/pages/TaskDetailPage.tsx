import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, Download, FileText } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { apiFetch } from '../lib/api';
import { PageTransition } from '../components/motion/primitives';
import { StateNoticeCard, TableSkeleton } from '../components/ui/Skeletons';
import '../styles/manual-reader.css';

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
  instruction_status: 'ready' | 'restricted' | 'unavailable' | 'source_permission_denied' | 'source_unavailable';
  attachments: string[];
}

const INSTRUCTION_NOTICES = {
  ready: '当前数据集版本未提供题干文件。',
  restricted: '该题题干尚未开放读取。',
  unavailable: '当前数据集版本未提供题干文件。',
  source_permission_denied: '评测服务无权读取题干来源。',
  source_unavailable: '题干来源暂时无法读取。',
} satisfies Record<TaskDetailResp['instruction_status'], string>;

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
          <ArrowLeft className="w-3.5 h-3.5" /> 返回数据集
        </Link>
        <div className="min-w-0">
          <h1 className="m-0 text-lg font-bold font-mono text-[var(--text-primary)] break-all">{taskId}</h1>
          <div className="text-xs font-mono text-[var(--text-muted)]">{datasetReleaseId}</div>
        </div>
      </div>

      {q.isLoading ? (
        <TableSkeleton rows={6} cols={2} />
      ) : q.isError ? (
        <StateNoticeCard kind="forbidden" title="题目详情不可访问" description={(q.error as Error).message} />
      ) : data ? (
        <>
          <section className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5 sm:p-7" aria-labelledby="dataset-task-instruction">
            <h2 id="dataset-task-instruction" className="m-0 mb-4 flex items-center gap-2 text-base font-bold text-[var(--text-primary)]">
              <FileText className="w-5 h-5 text-[var(--brand-primary)]" /> 题目内容
            </h2>
            {data.instruction ? (
              <article className="manual-prose break-words" aria-label="题干">
                <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} disallowedElements={['img']}>
                  {data.instruction}
                </ReactMarkdown>
              </article>
            ) : (
              <p className="m-0 text-sm text-[var(--text-muted)]">
                {INSTRUCTION_NOTICES[data.instruction_status] || INSTRUCTION_NOTICES.unavailable}
              </p>
            )}
            {data.attachments?.length > 0 && (
              <div className="mt-5 border-t border-[var(--border-subtle)] pt-4">
                <h3 className="m-0 mb-2 text-sm font-semibold">题目附件</h3>
                <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
                  {data.attachments.map((name) => (
                    <li className="min-w-0 max-w-full" key={name}>
                      <a
                        className="inline-flex items-center gap-2 break-all rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--brand-primary)]"
                        href={`/api/v1/datasets/${encodeURIComponent(datasetReleaseId)}/task-attachments/${encodeURIComponent(name)}?task_id=${encodeURIComponent(taskId)}`}
                        download
                      >
                        <Download className="h-4 w-4 shrink-0" /> {name}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4 grid grid-cols-2 sm:grid-cols-3 gap-3 text-xs">
            <div>
              <div className="text-[var(--text-muted)]">数据划分</div>
              <div className="mt-0.5 font-semibold">{data.split || 'default'}</div>
            </div>
            <div>
              <div className="text-[var(--text-muted)]">类别 / 难度</div>
              <div className="mt-0.5 font-semibold">
                {data.category || '—'} / {data.difficulty || '—'}
              </div>
            </div>
            <div>
              <div className="text-[var(--text-muted)]">校验值</div>
              <div className="mt-0.5 font-mono truncate">{data.task_checksum || '—'}</div>
            </div>
          </div>
        </>
      ) : null}
    </PageTransition>
  );
}
