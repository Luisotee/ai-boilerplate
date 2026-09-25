/**
 * Unit tests for utils/wa-markup-to-html.
 *
 * The stakes: an unbalanced tag makes Telegram reject the ENTIRE message with a
 * 400, so a false positive is much worse than a missed conversion. Most of these
 * cases therefore assert that something is *left alone*.
 */

import { describe, it, expect } from 'vitest';
import { waMarkupToHtml } from '../../src/utils/wa-markup-to-html.js';

describe('waMarkupToHtml', () => {
  describe('conversions', () => {
    it('converts bold', () => {
      expect(waMarkupToHtml('*bold*')).toBe('<b>bold</b>');
    });

    it('converts italic', () => {
      expect(waMarkupToHtml('_italic_')).toBe('<i>italic</i>');
    });

    it('converts strikethrough', () => {
      expect(waMarkupToHtml('~struck~')).toBe('<s>struck</s>');
    });

    it('converts inline code', () => {
      expect(waMarkupToHtml('`x = 1`')).toBe('<code>x = 1</code>');
    });

    it('converts a fenced block to <pre>', () => {
      expect(waMarkupToHtml('```\nx = 1\n```')).toBe('<pre>\nx = 1\n</pre>');
    });

    it('converts bold mid-sentence', () => {
      expect(waMarkupToHtml('There are *3 items* left.')).toBe('There are <b>3 items</b> left.');
    });

    it('converts multiple spans on one line', () => {
      expect(waMarkupToHtml('*a* e *b*')).toBe('<b>a</b> e <b>b</b>');
    });

    it('converts a single-character span', () => {
      expect(waMarkupToHtml('*!*')).toBe('<b>!</b>');
    });

    it('converts bold across separate lines independently', () => {
      expect(waMarkupToHtml('*um*\n*dois*')).toBe('<b>um</b>\n<b>dois</b>');
    });
  });

  describe('false positives that must be left alone', () => {
    it('leaves arithmetic alone', () => {
      expect(waMarkupToHtml('2*3')).toBe('2*3');
    });

    it('leaves spaced arithmetic alone', () => {
      expect(waMarkupToHtml('2 * 3 * 4')).toBe('2 * 3 * 4');
    });

    it('leaves snake_case identifiers alone', () => {
      expect(waMarkupToHtml('snake_case_name')).toBe('snake_case_name');
    });

    it('leaves a_b_c alone', () => {
      expect(waMarkupToHtml('a_b_c')).toBe('a_b_c');
    });

    it('leaves an unclosed delimiter alone', () => {
      expect(waMarkupToHtml('*unclosed')).toBe('*unclosed');
    });

    it('does not let a span cross a newline', () => {
      // The dangerous case: a stray `*` opening a list item would otherwise
      // swallow the remainder of the message into one <b>.
      expect(waMarkupToHtml('*a\nb*')).toBe('*a\nb*');
    });

    it('leaves an empty pair alone', () => {
      expect(waMarkupToHtml('**')).toBe('**');
    });

    it('leaves whitespace-hugging delimiters alone', () => {
      expect(waMarkupToHtml('* not bold *')).toBe('* not bold *');
    });

    it('leaves a bare asterisk alone', () => {
      expect(waMarkupToHtml('*')).toBe('*');
    });

    it('leaves a markdown-style bullet list alone', () => {
      const input = '* primeiro\n* segundo';
      expect(waMarkupToHtml(input)).toBe(input);
    });
  });

  describe('code spans are never re-parsed', () => {
    it('keeps asterisks literal inside inline code', () => {
      expect(waMarkupToHtml('`*not bold*`')).toBe('<code>*not bold*</code>');
    });

    it('keeps underscores literal inside inline code', () => {
      expect(waMarkupToHtml('`a_b_c`')).toBe('<code>a_b_c</code>');
    });

    it('keeps markup literal inside a fence', () => {
      expect(waMarkupToHtml('```\n*x* _y_\n```')).toBe('<pre>\n*x* _y_\n</pre>');
    });

    it('still converts markup outside a code span', () => {
      expect(waMarkupToHtml('*bold* and `code`')).toBe('<b>bold</b> and <code>code</code>');
    });

    it('handles several code spans', () => {
      expect(waMarkupToHtml('`a` then `b`')).toBe('<code>a</code> then <code>b</code>');
    });
  });

  describe('HTML escaping', () => {
    it('neutralizes a script tag', () => {
      expect(waMarkupToHtml('<script>alert(1)</script>')).toBe(
        '&lt;script&gt;alert(1)&lt;/script&gt;'
      );
    });

    it('escapes ampersands', () => {
      expect(waMarkupToHtml('Tom & Jerry')).toBe('Tom &amp; Jerry');
    });

    it('escapes angle brackets inside code exactly once', () => {
      expect(waMarkupToHtml('`<div>`')).toBe('<code>&lt;div&gt;</code>');
    });

    it('escapes an existing entity-looking string exactly once', () => {
      // Double-escaping would render as the literal text "&amp;lt;".
      expect(waMarkupToHtml('&lt;')).toBe('&amp;lt;');
    });

    it('escapes inside a converted span', () => {
      expect(waMarkupToHtml('*<b>*')).toBe('<b>&lt;b&gt;</b>');
    });
  });

  describe('realistic agent output', () => {
    it('renders a structured summary header as bold', () => {
      // The global prompt is WhatsApp-flavoured, so the model emits this shape;
      // it is the case that motivated doing the conversion client-side.
      const reply = '📋 *DAILY SUMMARY*\n\n*Location:* São Paulo - SP\n*Items:* 12\n\nStay tuned.';
      expect(waMarkupToHtml(reply)).toBe(
        '📋 <b>DAILY SUMMARY</b>\n\n<b>Location:</b> São Paulo - SP\n<b>Items:</b> 12\n\nStay tuned.'
      );
    });

    it('produces balanced tags for the summary', () => {
      const alert = '📋 *DAILY SUMMARY*\n\n*Location:* São Paulo - SP';
      const out = waMarkupToHtml(alert);
      expect((out.match(/<b>/g) ?? []).length).toBe((out.match(/<\/b>/g) ?? []).length);
    });
  });

  describe('edge cases', () => {
    it('handles empty input', () => {
      expect(waMarkupToHtml('')).toBe('');
    });

    it('leaves plain text untouched', () => {
      expect(waMarkupToHtml('olá, how are you?')).toBe('olá, how are you?');
    });

    it('preserves emoji and accents', () => {
      expect(waMarkupToHtml('*Attention* 🔥')).toBe('<b>Attention</b> 🔥');
    });

    it('never emits an unbalanced tag for adversarial delimiter runs', () => {
      // Whatever the rules do here, the output must be parseable.
      for (const input of ['***', '*_*_*', '~~~', '*a*b*', '_*x*_']) {
        const out = waMarkupToHtml(input);
        for (const tag of ['b', 'i', 's']) {
          const open = (out.match(new RegExp(`<${tag}>`, 'g')) ?? []).length;
          const close = (out.match(new RegExp(`</${tag}>`, 'g')) ?? []).length;
          expect(open, `unbalanced <${tag}> for input ${JSON.stringify(input)}`).toBe(close);
        }
      }
    });
  });
});
