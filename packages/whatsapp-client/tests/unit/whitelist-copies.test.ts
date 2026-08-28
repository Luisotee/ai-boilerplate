/**
 * The whitelist matcher is duplicated verbatim across the three TS clients by
 * design — the repo has no shared package, and the module is deliberately pure
 * (no config, no logger, no jid.ts import) precisely so the copies CAN be
 * identical. "Deliberately duplicated" only holds if drift is mechanically
 * impossible, so this test is the enforcement.
 *
 * If it fails: fix the matcher in whatsapp-client (the reference copy), then
 * `cp` it over the other two. Do not hand-merge — that is how they drift.
 *
 * whatsapp-client owns this check because it is the reference copy and the only
 * package whose suite exercises the matcher itself; the cloud and telegram
 * suites only cover their thin `passesWhitelist` wrappers.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const REPO_ROOT = resolve(import.meta.dirname, '../../../..');
const REFERENCE = 'packages/whatsapp-client/src/utils/whitelist.ts';
const COPIES = [
  'packages/whatsapp-cloud/src/utils/whitelist.ts',
  'packages/telegram-client/src/utils/whitelist.ts',
];

describe('whitelist.ts copies', () => {
  // Read as utf8 strings, not Buffers: vitest diffs a string mismatch down to
  // the drifted line, whereas a Buffer mismatch prints two byte arrays.
  const reference = readFileSync(resolve(REPO_ROOT, REFERENCE), 'utf8');

  it.each(COPIES)('%s is byte-identical to the reference', (copy) => {
    expect(readFileSync(resolve(REPO_ROOT, copy), 'utf8')).toBe(reference);
  });
});
