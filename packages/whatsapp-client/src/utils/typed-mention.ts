/**
 * Typed `@Name` mentions: an `@` the user typed by hand instead of picking the
 * autocomplete entry. WhatsApp sends those as plain text (no `mentionedJid`),
 * and Telegram tags them as a `mention` entity only when they look like a
 * username, so neither client sees them as addressed without this check.
 *
 * Pure and dependency-free so the copy in telegram-client stays byte-identical
 * (enforced by whatsapp-client `tests/unit/whitelist-copies.test.ts`). Fix it
 * here, then `cp` it over.
 *
 * Rules (from nanoclaw's `isBotTypedMention`):
 *   - the character before `@` must not be a letter, digit, `_`, `@`, `/` or
 *     `.`, so e-mail addresses and URLs never match;
 *   - the name must end at a Unicode word boundary, so `@José` matches `José`
 *     but `@Josélia` does not;
 *   - matching is case-insensitive;
 *   - names shorter than 2 characters are ignored (too easy to hit by chance).
 */

const MIN_NAME_LENGTH = 2;

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Usable, de-duplicated names (trimmed, a leading `@` removed). */
export function normalizeMentionNames(names: readonly (string | undefined | null)[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of names) {
    const name = (raw ?? '').trim().replace(/^@+/, '');
    if (name.length < MIN_NAME_LENGTH) continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(name);
  }
  return out;
}

/** Whether `text` contains `@<name>` for any of `names`, under the rules above. */
export function isTypedMention(
  text: string,
  names: readonly (string | undefined | null)[]
): boolean {
  if (!text || !text.includes('@')) return false;
  for (const name of normalizeMentionNames(names)) {
    const pattern = new RegExp(
      `(?:^|[^\\p{L}\\p{N}_@/.])@${escapeRegExp(name)}(?![\\p{L}\\p{N}_])`,
      'iu'
    );
    if (pattern.test(text)) return true;
  }
  return false;
}
