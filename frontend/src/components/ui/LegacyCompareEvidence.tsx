import { useId, useState, type ReactNode } from 'react';
import { BookOpen, Braces, ChevronDown } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { TableSkeleton } from './Skeletons';

export interface LegacyCompareSample {
  benchmark_dir: string;
  model_file: string;
  status: string;
  reward: number | null;
  sample: unknown;
}

interface EvidenceSide {
  label: string;
  data?: LegacyCompareSample;
  isLoading: boolean;
  error: Error | null;
}

const PREVIEW_LENGTH = 900;
const textStyle = 'm-0 [overflow-wrap:anywhere] text-sm leading-relaxed text-[var(--text-primary)]';
const cardStyle = 'min-w-0 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)]';

function asRecord(value: unknown) {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function asText(value: unknown) {
  return typeof value === 'string' ? value.trim() || undefined : undefined;
}

function questionText(sample: unknown) {
  const record = asRecord(sample);
  if (!record) return;
  const prompt = record.origin_prompt ?? record.prompt;
  if (Array.isArray(prompt)) {
    const messages = prompt.map((message) => {
      const turn = asRecord(message);
      return { role: asText(turn?.role), content: asText(turn?.content) };
    });
    if (messages.length === 1 && messages[0].content && (!messages[0].role || messages[0].role === 'user')) {
      return messages[0].content;
    }
    if (messages.length && messages.every(({ content }) => content)) {
      return messages.map(({ role, content }) => role ? role + '\n' + content : content).join('\n\n');
    }
  }
  const plainPrompt = asText(prompt);
  if (plainPrompt) return plainPrompt;

  const question = asText(record.question);
  if (!question) return;
  const options = Array.from('ABCDEFGHIJ').flatMap((key, index) => {
    const option = asText(record[key]) ?? asText(record['option' + (index + 1)]);
    return option ? [key + '. ' + option] : [];
  });
  return options.length ? [question, '', ...options].join('\n') : question;
}

function modelAnswer(sample: unknown): { text?: string; final: boolean } {
  const record = asRecord(sample);
  const prediction = asText(record?.prediction) ?? asText(record?.response) ?? asText(record?.output);
  if (!prediction) return { final: false };

  // Some historical exports omit the opening tag but keep the closing reasoning delimiter.
  const endOfThinking = /(^|\n)[ \t]*<\/think>[ \t]*(?=\r?\n|$)/.exec(prediction)
    ?? (prediction.startsWith('<think>') ? /<\/think>/.exec(prediction) : null);
  if (endOfThinking) {
    return { text: asText(prediction.slice(endOfThinking.index + endOfThinking[0].length)), final: true };
  }
  if (prediction.startsWith('<think>')) return { final: true };
  return { text: prediction, final: false };
}

function ExpandableText({ text, label, markdown = false }: { text: string; label: string; markdown?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const long = text.length > PREVIEW_LENGTH;
  const visibleText = long && !expanded ? text.slice(0, PREVIEW_LENGTH).trimEnd() + '…' : text;
  return (
    <>
      {markdown && (
        <div id={id} className={textStyle + ' space-y-2.5 [&_ol]:list-decimal [&_ul]:list-disc [&_ol]:pl-5 [&_ul]:pl-5 [&_li+li]:mt-1 [&_pre]:overflow-auto [&_pre]:rounded [&_pre]:bg-[var(--bg-surface)] [&_pre]:p-2'}>
          <ReactMarkdown skipHtml disallowedElements={['a', 'img']} unwrapDisallowed>{visibleText}</ReactMarkdown>
        </div>
      )}
      {!markdown && <p id={id} className={textStyle + ' whitespace-pre-wrap'}>{visibleText}</p>}
      {long && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((value) => !value)}
          className="mt-2 inline-flex items-center gap-1 rounded py-1 text-xs font-medium text-[var(--brand-primary)] hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 cursor-pointer"
        >
          <ChevronDown aria-hidden="true" className={expanded ? 'size-3.5 rotate-180' : 'size-3.5'} />
          {expanded ? '收起' + label : '展开完整' + label}
        </button>
      )}
    </>
  );
}

function EvidenceDetails({ label, children }: { label: ReactNode; children: () => ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="group/evidence rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-surface)]"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2.5 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-[var(--brand-primary)] [&::-webkit-details-marker]:hidden">
        {label}
        <ChevronDown aria-hidden="true" className="ml-auto size-3.5 shrink-0 group-open/evidence:rotate-180" />
      </summary>
      {open && children()}
    </details>
  );
}

function QuestionBody({ prompt, benchmarkDir }: { prompt: string; benchmarkDir?: string }) {
  const documents = benchmarkDir === 'AA-LCR-v1.1'
    ? /^BEGIN INPUT DOCUMENTS[ \t]*\r?\n([\s\S]*?)\r?\nEND INPUT DOCUMENTS[ \t]*\r?\n([\s\S]+)$/.exec(prompt)
    : null;
  const remainder = documents?.[2].trim() || prompt;
  const markedQuestion = documents
    ? /^Answer the following question using the input documents provided above\.\s+START QUESTION\s+([\s\S]+?)\s+END QUESTION$/.exec(remainder)
    : null;
  const question = markedQuestion?.[1] ?? remainder;
  return (
    <>
      <ExpandableText text={question} label="题面" />
      {documents && (
        <div className="mt-3">
          <EvidenceDetails label="题目所附文档">
            {() => (
              <div tabIndex={0} className="max-h-[45vh] overflow-y-auto border-t border-[var(--border-subtle)] p-3">
                <p className={textStyle + ' whitespace-pre-wrap'}>{documents[1]}</p>
              </div>
            )}
          </EvidenceDetails>
        </div>
      )}
    </>
  );
}

export function LegacyCompareEvidence({ a, b }: { a: EvidenceSide; b: EvidenceSide }) {
  const promptA = questionText(a.data?.sample);
  const promptB = questionText(b.data?.sample);
  const common = Boolean(promptA && promptA === promptB);
  const sides = [{ id: 'A', ...a, prompt: promptA }, { id: 'B', ...b, prompt: promptB }];
  const hasRawSample = sides.some(({ data }) => data?.sample != null);
  return (
    <div className="space-y-4">
      {(promptA || promptB) && (
        <section aria-label={common ? '共用题面' : '题面'} className={cardStyle + ' p-4 sm:p-5'}>
          <h3 className="m-0 mb-3 flex items-center gap-2 text-sm font-semibold text-[var(--text-primary)]">
            <BookOpen aria-hidden="true" className="size-4 shrink-0 text-[var(--brand-primary)]" />
            {common ? '共用题面' : '题面'}
          </h3>
          {common && promptA ? (
            <QuestionBody prompt={promptA} benchmarkDir={a.data?.benchmark_dir} />
          ) : (
            <div className="space-y-4">
              {sides.map((side) => side.prompt && (
                <div key={side.id}>
                  <p className="m-0 mb-1.5 text-xs font-medium text-[var(--text-secondary)]">{side.id} · {side.label}</p>
                  <QuestionBody prompt={side.prompt} benchmarkDir={side.data?.benchmark_dir} />
                </div>
              ))}
            </div>
          )}
        </section>
      )}
      <div className="grid items-start gap-4 md:grid-cols-2">
        {sides.map(({ id, label, data, isLoading, error }) => {
          const answer = modelAnswer(data?.sample);
          return (
            <section key={id} aria-label={id + ' 方回答'} className={cardStyle + ' overflow-hidden'}>
              <div className="flex items-start gap-2.5 border-b border-[var(--border-subtle)] p-4">
                <span aria-hidden="true" className={'grid size-7 shrink-0 place-items-center rounded-lg text-xs font-bold ' + (id === 'A'
                  ? 'bg-[var(--brand-tint)] text-[var(--brand-primary)]'
                  : 'bg-[var(--sem-legacy-bg)] text-[var(--sem-legacy-fg)]')}>{id}</span>
                <div className="min-w-0 flex-1">
                  <h3 className="m-0 text-sm font-semibold [overflow-wrap:anywhere] text-[var(--text-primary)]">{label}</h3>
                  {data && (
                    <p className="m-0 mt-1 text-xs text-[var(--text-secondary)]">
                      样本评分 <span className="font-semibold tabular-nums text-[var(--text-primary)]">{data.reward ?? '未记录'}</span>
                      {data.status !== 'recorded' && data.status !== 'scored' && data.status && (
                        <span> · {data.status}</span>
                      )}
                    </p>
                  )}
                </div>
              </div>
              <div className="p-4">
                {isLoading ? (
                  <TableSkeleton rows={3} cols={1} />
                ) : error || !data ? (
                  <p className="m-0 text-sm text-[var(--text-secondary)]">{error?.message || '无法加载该侧明细'}</p>
                ) : (
                  <>
                    <h4 className="m-0 mb-2 text-xs font-semibold text-[var(--text-secondary)]">{answer.final ? '最终回答' : '模型输出'}</h4>
                    {answer.text
                      ? <ExpandableText text={answer.text} label="回答" markdown={answer.final} />
                      : <p className="m-0 text-sm text-[var(--text-muted)]">{answer.final ? '未记录独立的最终回答' : '未识别到回答正文'}</p>}
                  </>
                )}
              </div>
            </section>
          );
        })}
      </div>
      {hasRawSample && (
        <EvidenceDetails label={<><Braces aria-hidden="true" className="size-4 shrink-0" /> 原始 JSON</>}>
          {() => (
            <div className="grid gap-3 border-t border-[var(--border-subtle)] p-3 md:grid-cols-2">
              {sides.map(({ id, label, data }) => data && data.sample != null && (
                <div key={id} className="min-w-0">
                  <p className="m-0 mb-1.5 text-xs [overflow-wrap:anywhere] text-[var(--text-secondary)]">{id} · {data.model_file || label}</p>
                  <pre tabIndex={0} className="m-0 max-h-[45vh] overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere] rounded-md bg-[var(--bg-elevated)] p-3 text-xs leading-relaxed text-[var(--text-primary)]">
                    {JSON.stringify(data.sample, null, 2)}
                  </pre>
                </div>
              ))}
            </div>
          )}
        </EvidenceDetails>
      )}
    </div>
  );
}
