export interface SplitOptions {
  maxChunks?: number;
  disabled?: boolean;
}

const DELIMITER_LINE_RE = /^---\s*$/;
const FENCE_RE = /^```/;
const HARD_CAP = 10;
const DEFAULT_MAX_CHUNKS = 5;

/**
 * Telegram's `sendMessage` hard limit: 1-4096 characters after entities are
 * parsed. Exceeding it is a 400 that no retry can fix, and it is NOT the
 * `can't parse entities` error, so the plain-text fallback doesn't catch it —
 * the whole reply is discarded.
 *
 * Splitting is applied to the RAW text, before `waMarkupToHtml`. That keeps
 * markup tags balanced inside each chunk, and is conservative: HTML tags are
 * consumed as entities and don't count toward the limit, so a chunk that fits
 * before conversion also fits after.
 */
export const MAX_MESSAGE_CHARS = 4096;

function parseDelimiterLines(text: string): {
  lines: string[];
  boundaries: number[];
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
  return { lines, boundaries };
}

/**
 * Break a single over-long string into pieces of at most `limit` characters,
 * preferring the least disruptive boundary available: paragraph, then line,
 * then word, then a hard cut for input with no whitespace at all (a long URL,
 * a base64 blob).
 *
 * Always makes progress — every branch consumes at least one character — so it
 * cannot loop forever on pathological input.
 */
export function splitByLength(text: string, limit: number = MAX_MESSAGE_CHARS): string[] {
  if (limit <= 0) return [text];
  if (text.length <= limit) return [text];

  const chunks: string[] = [];
  let rest = text;

  while (rest.length > limit) {
    const window = rest.slice(0, limit + 1);
    // Prefer the last boundary that still leaves a non-empty chunk.
    let cut = window.lastIndexOf('\n\n');
    if (cut <= 0) cut = window.lastIndexOf('\n');
    if (cut <= 0) cut = window.lastIndexOf(' ');
    if (cut <= 0) cut = limit; // no whitespace to break on — hard cut

    const piece = rest.slice(0, cut);
    chunks.push(piece.trim() || piece);
    rest = rest.slice(cut).replace(/^\s+/, '');
    if (rest.length === 0) break;
  }

  if (rest.length > 0) chunks.push(rest);
  return chunks.filter((c) => c.length > 0);
}

export function splitResponseIntoBursts(text: string, options: SplitOptions = {}): string[] {
  const disabled = options.disabled ?? false;
  const rawMax = options.maxChunks;
  const safeMax =
    typeof rawMax === 'number' && Number.isFinite(rawMax) ? rawMax : DEFAULT_MAX_CHUNKS;
  const maxChunks = Math.max(1, Math.min(safeMax, HARD_CAP));

  // The length cap applies on EVERY path, including `disabled`. Bursting is
  // turned off for groups, which is exactly where a long answer used to be
  // returned as one over-limit chunk and rejected wholesale by Telegram.
  if (disabled) return splitByLength(stripSplitDelimiters(text));
  if (!text.trim()) return [text];

  const { lines, boundaries } = parseDelimiterLines(text);

  if (boundaries.length === 0) return splitByLength(text);

  const parts: string[] = [];
  let start = 0;
  for (const boundary of boundaries) {
    parts.push(lines.slice(start, boundary).join('\n'));
    start = boundary + 1;
  }
  parts.push(lines.slice(start).join('\n'));

  const nonEmpty = parts.map((p) => p.trim()).filter((p) => p.length > 0);

  if (nonEmpty.length <= 1) return splitByLength(stripSplitDelimiters(text));

  // Note the tail merge below can produce an over-long chunk even when every
  // individual part fit, so the length pass has to run after it, not before.
  const burst =
    nonEmpty.length > maxChunks
      ? [...nonEmpty.slice(0, maxChunks - 1), nonEmpty.slice(maxChunks - 1).join('\n\n')]
      : nonEmpty;

  return burst.flatMap((chunk) => splitByLength(chunk));
}

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
