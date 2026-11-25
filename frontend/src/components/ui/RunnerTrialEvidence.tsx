import { StateNoticeCard } from './Skeletons';

const INLINE_IMAGE = /^data:image\/(?:jpeg|png|webp|gif);base64,[a-z0-9+/=\r\n]+$/i;

interface PromptPart {
  type?: string;
  text?: string;
  image_url?: { url?: string };
}

interface PromptMessage {
  role?: string;
  content?: string | PromptPart[];
}

function TextEvidence({ title, value }: { title: string; value: unknown }) {
  if (typeof value !== 'string' || !value.trim()) return null;
  return (
    <section className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
      <h3 className="m-0 mb-2 text-xs font-bold text-[var(--text-primary)]">{title}</h3>
      <pre className="m-0 max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[var(--bg-elevated)] p-3 text-xs">
        {value}
      </pre>
    </section>
  );
}

export function RunnerTrialEvidence({
  evidence,
  verifier,
}: {
  evidence: Record<string, unknown>;
  verifier: boolean;
}) {
  if (verifier) {
    if (evidence.verifier_log_redacted) {
      return <StateNoticeCard kind="forbidden" title="验证器日志受限" description="当前账号缺少验证器日志读取权限。" />;
    }
    if (!evidence.verifier_log) {
      return <StateNoticeCard title="暂无验证器日志" description="这次运行未采集验证器日志。" />;
    }
    return <TextEvidence title="验证器日志" value={evidence.verifier_log} />;
  }

  const messages = Array.isArray(evidence.prompt) ? (evidence.prompt as (PromptMessage | null)[]) : [];
  const parts: PromptPart[] = messages.flatMap((message) => {
    if (typeof message?.content === 'string') return [{ type: 'text', text: message.content }];
    return Array.isArray(message?.content) ? message.content.filter(Boolean) : [];
  });
  const prompt = typeof evidence.prompt === 'string'
    ? evidence.prompt
    : parts.filter((part) => part.type === 'text' && typeof part.text === 'string').map((part) => part.text).join('\n\n');
  const images = parts.flatMap((part) => {
    const url = part.type === 'image_url' ? part.image_url?.url : undefined;
    return typeof url === 'string' && INLINE_IMAGE.test(url) ? [url] : [];
  });
  if (!prompt && images.length === 0 && !evidence.response && !evidence.answer) {
    return <StateNoticeCard title="暂无问答记录" description="这次运行未采集题面、图像或模型回答。" />;
  }

  return (
    <div className="space-y-4">
      <TextEvidence title="题面" value={prompt} />
      {images.length > 0 && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {images.map((url, index) => (
            <img
              key={index}
              src={url}
              alt={`题面图像 ${index + 1}`}
              className="max-h-96 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-surface)] object-contain p-2"
            />
          ))}
        </div>
      )}
      <TextEvidence title="模型回答" value={evidence.response} />
      <TextEvidence title="参考答案" value={evidence.answer} />
    </div>
  );
}
