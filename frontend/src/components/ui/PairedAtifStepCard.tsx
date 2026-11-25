import React, { useEffect, useState } from 'react';
import { AlertTriangle, Brain, Check, ChevronDown, ChevronRight, Copy, Terminal } from 'lucide-react';
import type { AtifStep } from '../../types/api';
import { CollapsiblePanel } from '../motion/primitives';

export interface PairedAtifStepCardProps {
  step: AtifStep;
  highlighted?: boolean;
  globalReasoningOpen?: boolean;
  instantToggle?: boolean;
  onCopyAnchor?: (stepId: number) => void;
}

/**
 * Bespoke UI Element #4: PairedAtifStepCard (v2.2.1 01-1 §2.1.6 & 01-2 §2.4)
 * Left 2px semantic connector rail pairing tool_call <-> observation with bidirectional hover,
 * collapsible reasoning accordion (0ms instant switch on batch 't' toggle for 100+ steps).
 */
export function PairedAtifStepCard({
  step,
  highlighted = false,
  globalReasoningOpen,
  instantToggle = false,
  onCopyAnchor,
}: PairedAtifStepCardProps) {
  const [reasonOpen, setReasonOpen] = useState(false);
  const [hoveredCallId, setHoveredCallId] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    if (globalReasoningOpen !== undefined) {
      setReasonOpen(globalReasoningOpen);
    }
  }, [globalReasoningOpen]);

  const hasError = step.paired_tools.some((t) => t.observation?.is_error);

  const copyText = (key: string, txt: string) => {
    navigator.clipboard?.writeText(txt).catch(() => {});
    setCopiedKey(key);
    window.setTimeout(() => setCopiedKey((cur) => (cur === key ? null : cur)), 1500);
  };

  const renderMessageText = (msg: unknown): string | null => {
    if (msg === null || msg === undefined) return null;
    if (typeof msg === 'string') return msg;
    try {
      return JSON.stringify(msg, null, 2);
    } catch {
      return String(msg);
    }
  };

  const msgText = renderMessageText(step.message);

  return (
    <div
      id={`step-${step.step_id}`}
      className={`rounded-xl border bg-[var(--bg-surface)] p-3.5 transition-colors ${
        hasError ? 'border-[var(--sem-err-border)]' : 'border-[var(--border-subtle)]'
      } ${highlighted ? 'lrm-step-flash' : ''}`}
    >
      <div className="flex items-center justify-between gap-2 pb-2 mb-2 border-b border-[var(--border-subtle)]">
        <div className="flex items-center gap-2 text-xs">
          <button
            type="button"
            onClick={() => onCopyAnchor?.(step.step_id)}
            className="font-mono font-semibold text-[var(--brand-primary)] hover:underline cursor-pointer"
            title="复制步骤直链锚点"
          >
            #step-{step.step_id}
          </button>
          <span className="rounded px-1.5 py-0.5 text-[11px] font-medium bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
            {step.source || 'agent'}
          </span>
          {hasError && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium bg-[var(--sem-err-bg)] text-[var(--sem-err-fg)]">
              <AlertTriangle className="w-3 h-3" /> 工具报错
            </span>
          )}
        </div>
        {step.timestamp && (
          <span className="text-[11px] font-mono tabular-nums text-[var(--text-muted)]">
            {step.timestamp.replace('T', ' ').slice(0, 19)}
          </span>
        )}
      </div>

      {step.reasoning_content && (
        <div className="mb-2.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)]">
          <button
            type="button"
            onClick={() => setReasonOpen((v) => !v)}
            className="w-full flex items-center justify-between px-3 py-1.5 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
          >
            <span className="inline-flex items-center gap-1.5">
              <Brain className="w-3.5 h-3.5 text-[var(--sem-legacy-fg)]" />
              思考过程 (reasoning_content · {step.reasoning_content.length.toLocaleString()} chars)
            </span>
            {reasonOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          </button>
          <CollapsiblePanel open={reasonOpen} instant={instantToggle}>
            <pre className="m-0 px-3 pb-2.5 pt-1 text-xs leading-relaxed whitespace-pre-wrap break-words text-[var(--text-secondary)] border-t border-[var(--border-subtle)] max-h-80 overflow-y-auto">
              {step.reasoning_content}
            </pre>
          </CollapsiblePanel>
        </div>
      )}

      {msgText && msgText.trim() !== '' && (
        <div className="mb-2.5 text-xs leading-relaxed whitespace-pre-wrap break-words text-[var(--text-primary)]">
          {msgText}
        </div>
      )}

      {step.paired_tools.length > 0 && (
        <div className="space-y-2.5">
          {step.paired_tools.map((tc, idx) => {
            const callKey = tc.tool_call_id || `tc_${idx}`;
            const isActive = hoveredCallId === callKey;
            const isErr = Boolean(tc.observation?.is_error);
            const argStr =
              typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments ?? {}, null, 2);
            const obsStr = tc.observation?.content ?? null;

            return (
              <div
                key={callKey}
                onMouseEnter={() => setHoveredCallId(callKey)}
                onMouseLeave={() => setHoveredCallId(null)}
                className="relative pl-3 rounded-lg transition-colors"
                style={{
                  borderLeft: `2px solid ${
                    isErr ? 'var(--sem-err-fg)' : isActive ? 'var(--brand-primary)' : 'var(--border-strong)'
                  }`,
                }}
              >
                <div
                  className={`rounded-md border p-2 transition-colors ${
                    isActive
                      ? 'border-[var(--brand-primary)] bg-[var(--brand-tint)]/35'
                      : 'border-[var(--border-subtle)] bg-[var(--bg-elevated)]'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="inline-flex items-center gap-1.5 text-xs font-mono font-semibold text-[var(--text-primary)]">
                      <Terminal className="w-3.5 h-3.5 text-[var(--brand-primary)]" />
                      {tc.function_name || 'tool_call'}
                    </span>
                    <button
                      type="button"
                      onClick={() => copyText(callKey, argStr)}
                      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-surface)] cursor-pointer"
                    >
                      {copiedKey === callKey ? <Check className="w-3 h-3 text-[var(--sem-pass-fg)]" /> : <Copy className="w-3 h-3" />}
                      <span>{copiedKey === callKey ? '已复制' : '复制'}</span>
                    </button>
                  </div>
                  <pre className="m-0 text-[11px] font-mono whitespace-pre-wrap break-all text-[var(--text-secondary)] max-h-48 overflow-y-auto">
                    {argStr}
                  </pre>
                </div>

                {tc.observation && (
                  <div
                    className={`mt-1.5 rounded-md border p-2 ${
                      isErr
                        ? 'border-[var(--sem-err-border)] bg-[var(--sem-err-bg)]/50'
                        : isActive
                          ? 'border-[var(--brand-primary)]/60 bg-[var(--bg-surface)]'
                          : 'border-[var(--border-subtle)] bg-[var(--bg-surface)]'
                    }`}
                  >
                    <div className="flex items-center justify-between text-[11px] mb-1">
                      <span className={`font-medium ${isErr ? 'text-[var(--sem-err-fg)]' : 'text-[var(--text-secondary)]'}`}>
                        Observation {isErr ? '(ERROR)' : '(OK)'}
                      </span>
                      {obsStr && (
                        <button
                          type="button"
                          onClick={() => copyText(`${callKey}_obs`, obsStr)}
                          className="inline-flex items-center gap-1 text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
                        >
                          {copiedKey === `${callKey}_obs` ? <Check className="w-3 h-3 text-[var(--sem-pass-fg)]" /> : <Copy className="w-3 h-3" />}
                        </button>
                      )}
                    </div>
                    <pre className="m-0 text-[11px] font-mono whitespace-pre-wrap break-words text-[var(--text-primary)] max-h-56 overflow-y-auto">
                      {obsStr || '(empty output)'}
                    </pre>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
