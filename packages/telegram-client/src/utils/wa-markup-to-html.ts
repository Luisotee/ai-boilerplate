/**
 * Convert WhatsApp-flavoured markup to Telegram HTML.
 *
 * The agent's system prompt is WhatsApp-flavoured and lives in a single global
 * `bot_prompt` row shared by every client, so the model emits `*bold*`,
 * `_italic_`, `~strike~` and `` `code` `` regardless of platform. WhatsApp
 * renders those natively; Telegram would show the literal asterisks. Translating
 * here keeps the prompt global instead of adding per-platform syntax rules.
 *
 * Why HTML and not MarkdownV2: MarkdownV2 requires escaping 18 characters
 * (`_*[]()~`>#+-=|{}.!`), so a single unescaped `.` or `-` in model output is a
 * 400 for the whole message. HTML needs only `&`, `<` and `>`.
 *
 * Deliberately conservative — an unbalanced tag fails the entire send, so the
 * rules only fire on a balanced, same-line, non-empty pair with no whitespace
 * against the delimiters, and never when the delimiter is flanked by a word
 * character. Anything ambiguous is left as literal text. `sendText`
 * additionally retries once without `parse_mode` if Telegram still objects.
 */

/** Escape the three characters Telegram's HTML parser treats as markup. */
function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Unicode private-use sentinels. They can't occur in model output, survive
// escapeHtml untouched (no & < >), and contain no markup characters, so the
// emphasis passes step straight over a lifted code span.
const OPEN = '';
const CLOSE = '';

/**
 * Lift code spans out of the raw text before anything else runs, so that
 * `` `*not bold*` `` keeps its asterisks and `<pre>` bodies are escaped exactly
 * once. Triple fences are matched before single backticks.
 */
function extractCode(text: string): { text: string; blocks: string[] } {
  const blocks: string[] = [];
  const withPlaceholders = text.replace(
    /```([\s\S]*?)```|`([^`\n]+)`/g,
    (_match, fenced: string | undefined, inline: string | undefined) => {
      const isFence = fenced !== undefined;
      const body = isFence ? fenced : (inline as string);
      const tag = isFence ? 'pre' : 'code';
      blocks.push(`<${tag}>${escapeHtml(body)}</${tag}>`);
      return `${OPEN}${blocks.length - 1}${CLOSE}`;
    }
  );
  return { text: withPlaceholders, blocks };
}

function restoreCode(text: string, blocks: string[]): string {
  return text.replace(
    new RegExp(`${OPEN}(\\d+)${CLOSE}`, 'g'),
    (_m, i: string) => blocks[Number(i)] ?? ''
  );
}

/**
 * Apply one emphasis rule.
 *
 * Guards, each protecting against a real false positive seen in model output:
 *  - non-empty content (`**` is not bold)
 *  - no whitespace adjacent to the delimiters (`2 * 3 * 4` is arithmetic)
 *  - content cannot span lines (an unclosed `*` opening a list item would
 *    otherwise swallow the rest of the message)
 *  - the delimiter cannot be flanked by a word character, so `snake_case`,
 *    `a_b_c` and `2*3` survive untouched
 */
function applyEmphasis(text: string, delimiter: string, tag: string): string {
  const d = delimiter.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(
    `(^|[^\\w${d}])${d}([^\\s${d}](?:[^\\n${d}]*[^\\s${d}])?)${d}(?=[^\\w${d}]|$)`,
    'g'
  );
  return text.replace(
    pattern,
    (_m, before: string, body: string) => `${before}<${tag}>${body}</${tag}>`
  );
}

/**
 * Convert WhatsApp markup in `text` to Telegram HTML.
 *
 * Order matters: lift code spans out of the raw text first (so their bodies are
 * escaped once and never re-parsed), then escape the remainder — which is what
 * makes model-authored `<script>` inert — then emphasis, then restore.
 */
export function waMarkupToHtml(text: string): string {
  const { text: withoutCode, blocks } = extractCode(text);

  let out = escapeHtml(withoutCode);
  out = applyEmphasis(out, '*', 'b');
  out = applyEmphasis(out, '_', 'i');
  out = applyEmphasis(out, '~', 's');

  return restoreCode(out, blocks);
}
