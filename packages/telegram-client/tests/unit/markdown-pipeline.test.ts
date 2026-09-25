/**
 * End-to-end check of the outbound formatting pipeline for Telegram:
 *
 *   model Markdown → WhatsApp markup (ai-api `formatting.markdown_to_whatsapp`)
 *                  → `---` bursts + Telegram HTML (this client)
 *
 * The fixture lives in the ai-api package and is asserted from BOTH sides: the
 * Python test (`tests/unit/test_formatting.py`) pins the first hop, this test
 * pins the second. Either side changing its output breaks one of them, so the
 * two converters can't drift apart silently.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { waMarkupToHtml } from '../../src/utils/wa-markup-to-html.js';
import { splitResponseIntoBursts } from '../../src/utils/message-split.js';

interface PipelineCase {
  name: string;
  markdown: string;
  whatsapp: string;
  telegram_html: string[];
}

const FIXTURE = fileURLToPath(
  new URL('../../../ai-api/tests/fixtures/markdown_pipeline.json', import.meta.url)
);
const cases: PipelineCase[] = JSON.parse(readFileSync(FIXTURE, 'utf8'));

describe('Markdown → WhatsApp markup → Telegram HTML pipeline', () => {
  it('has cases', () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  for (const c of cases) {
    it(`renders ${c.name}`, () => {
      const html = splitResponseIntoBursts(c.whatsapp).map(waMarkupToHtml);
      expect(html).toEqual(c.telegram_html);
    });
  }

  it('never leaks Markdown bold or link syntax into Telegram HTML', () => {
    for (const c of cases) {
      for (const chunk of splitResponseIntoBursts(c.whatsapp).map(waMarkupToHtml)) {
        // Code spans may legitimately contain `**`; strip them first.
        const outsideCode = chunk.replace(/<code>[\s\S]*?<\/code>|<pre>[\s\S]*?<\/pre>/g, '');
        expect(outsideCode, c.name).not.toContain('**');
        expect(outsideCode, c.name).not.toContain('](');
      }
    }
  });

  it('keeps the `---` burst delimiters produced by the ai-api hop', () => {
    const bursts = cases.find((c) => c.name === 'bursts');
    expect(bursts?.telegram_html).toHaveLength(3);
  });
});
