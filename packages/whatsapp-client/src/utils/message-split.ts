import { logger } from '../logger.js';

export interface SplitOptions {
  maxChunks?: number;
  disabled?: boolean;
}

const DELIMITER_LINE_RE = /^---\s*$/;
const FENCE_RE = /^```/;
const HARD_CAP = 10;
const DEFAULT_MAX_CHUNKS = 5;

function parseDelimiterLines(text: string): {
  lines: string[];
  boundaries: number[];
  unclosedFence: boolean;
} {
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
  return { lines, boundaries, unclosedFence: inFence };
}

/**
 * Lines inside fenced code blocks (``` … ```) are never treated as delimiters.
 * Returns a single-element, delimiter-free array when splitting is skipped so
 * callers can use the same send loop for split and non-split responses.
 */
export function splitResponseIntoBursts(text: string, options: SplitOptions = {}): string[] {
  const disabled = options.disabled ?? false;
  const rawMax = options.maxChunks;
  const safeMax =
    typeof rawMax === 'number' && Number.isFinite(rawMax) ? rawMax : DEFAULT_MAX_CHUNKS;
  const maxChunks = Math.max(1, Math.min(safeMax, HARD_CAP));

  if (disabled) return [stripSplitDelimiters(text)];
  if (!text.trim()) return [text];

  const { lines, boundaries, unclosedFence } = parseDelimiterLines(text);

  if (unclosedFence) {
    logger.warn(
      { preview: text.slice(0, 100) },
      'Unclosed fenced code block in AI response; splitting may be unreliable'
    );
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

  if (nonEmpty.length <= 1) return [stripSplitDelimiters(text)];

  if (nonEmpty.length > maxChunks) {
    const head = nonEmpty.slice(0, maxChunks - 1);
    const tail = nonEmpty.slice(maxChunks - 1).join('\n\n');
    return [...head, tail];
  }

  return nonEmpty;
}

/**
 * Remove `---` split markers outside fenced code blocks and collapse the
 * resulting blank-line runs. Used by consumers (e.g. TTS) that must not see
 * the delimiter.
 */
export function stripSplitDelimiters(text: string): string {
  if (!text) return text;
  const { lines, boundaries } = parseDelimiterLines(text);
  const boundarySet = new Set(boundaries);
  const kept =
    boundaries.length > 0 ? lines.map((line, i) => (boundarySet.has(i) ? '' : line)) : lines;
  return kept
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export function sleep(ms: number): Promise<void> {
  const safeMs = Number.isFinite(ms) && ms >= 0 ? ms : 0;
  return new Promise((resolve) => setTimeout(resolve, safeMs));
}
