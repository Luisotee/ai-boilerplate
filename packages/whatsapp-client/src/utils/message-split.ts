export interface SplitOptions {
  maxChunks?: number;
  disabled?: boolean;
}

const DELIMITER_LINE_RE = /^---\s*$/;
const DELIMITER_LINE_GLOBAL_RE = /^---\s*$/gm;
const FENCE_RE = /^```/;
const HARD_CAP = 10;
const DEFAULT_MAX_CHUNKS = 5;

/**
 * Split an AI response into burst-style WhatsApp messages.
 *
 * The agent marks message boundaries with a line containing only `---`.
 * Lines inside fenced code blocks (``` … ```) are never treated as delimiters.
 * Returns a single-element array when the delimiter is absent, so callers can
 * use the same send loop for split and non-split responses.
 */
export function splitResponseIntoBursts(text: string, options: SplitOptions = {}): string[] {
  const disabled = options.disabled ?? false;
  const maxChunks = Math.max(1, Math.min(options.maxChunks ?? DEFAULT_MAX_CHUNKS, HARD_CAP));

  if (disabled || !text.trim()) return [text];

  const lines = text.split('\n');
  const boundaries: number[] = [];
  let inFence = false;

  for (let i = 0; i < lines.length; i++) {
    if (FENCE_RE.test(lines[i])) {
      inFence = !inFence;
      continue;
    }
    if (!inFence && DELIMITER_LINE_RE.test(lines[i])) {
      boundaries.push(i);
    }
  }

  if (boundaries.length === 0) return [text];

  const parts: string[] = [];
  let start = 0;
  for (const boundary of boundaries) {
    parts.push(lines.slice(start, boundary).join('\n'));
    start = boundary + 1;
  }
  parts.push(lines.slice(start).join('\n'));

  const nonEmpty = parts.map((p) => p.trim()).filter((p) => p.length > 0);

  if (nonEmpty.length <= 1) return [text];

  if (nonEmpty.length > maxChunks) {
    const head = nonEmpty.slice(0, maxChunks - 1);
    const tail = nonEmpty.slice(maxChunks - 1).join('\n\n');
    return [...head, tail];
  }

  return nonEmpty;
}

/**
 * Remove `---` split markers so the text can be fed to TTS or other consumers
 * that should never see the visual delimiter.
 */
export function stripSplitDelimiters(text: string): string {
  return text
    .replace(DELIMITER_LINE_GLOBAL_RE, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
