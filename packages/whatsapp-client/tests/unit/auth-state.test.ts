/**
 * Crash-safe creds.json: atomic writes with a .bak, restored at startup when
 * creds.json doesn't parse (a truncated file would otherwise force a re-pair).
 * Uses a real temp directory and the real Baileys useMultiFileAuthState.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

vi.mock('../../src/logger.js', () => ({
  logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), debug: vi.fn() },
}));

type Mod = typeof import('../../src/services/auth-state.js');
let mod: Mod;
let dir: string;

const credsPath = () => join(dir, 'creds.json');
const readJson = async (p: string) => JSON.parse(await readFile(p, 'utf-8'));

describe('atomic auth state', () => {
  beforeEach(async () => {
    vi.resetModules();
    mod = await import('../../src/services/auth-state.js');
    dir = await mkdtemp(join(tmpdir(), 'auth-state-'));
  });

  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it('saves creds as valid JSON via a temp file and keeps the previous copy as .bak', async () => {
    const { state, saveCreds } = await mod.useAtomicMultiFileAuthState(dir);
    state.creds.registrationId = 111;
    await saveCreds();
    state.creds.registrationId = 222;
    await saveCreds();

    expect((await readJson(credsPath())).registrationId).toBe(222);
    expect((await readJson(`${credsPath()}.bak`)).registrationId).toBe(111);
    expect(await readdir(dir)).not.toContain('creds.json.tmp');
  });

  it('round-trips Buffers through the saved file (BufferJSON)', async () => {
    const { state, saveCreds } = await mod.useAtomicMultiFileAuthState(dir);
    await saveCreds();
    vi.resetModules();
    const fresh = await import('../../src/services/auth-state.js');
    const { state: reloaded } = await fresh.useAtomicMultiFileAuthState(dir);
    expect(Buffer.from(reloaded.creds.noiseKey.private)).toEqual(
      Buffer.from(state.creds.noiseKey.private)
    );
  });

  it('restores a truncated creds.json from the backup at startup', async () => {
    const { state, saveCreds } = await mod.useAtomicMultiFileAuthState(dir);
    state.creds.registrationId = 111;
    await saveCreds();
    await saveCreds(); // .bak now holds a good copy
    await writeFile(credsPath(), ''); // the power cut

    const { state: reloaded } = await mod.useAtomicMultiFileAuthState(dir);
    expect(reloaded.creds.registrationId).toBe(111);
    expect((await readJson(credsPath())).registrationId).toBe(111);
  });

  it('leaves a corrupt creds.json alone when there is no usable backup', async () => {
    await writeFile(credsPath(), '{"trunc');
    expect(await mod.restoreCredsBackupIfCorrupt(dir)).toBe(false);
    expect(await readFile(credsPath(), 'utf-8')).toBe('{"trunc');
  });

  it('does nothing when creds.json is valid or absent', async () => {
    expect(await mod.restoreCredsBackupIfCorrupt(dir)).toBe(false);
    await writeFile(credsPath(), '{}');
    expect(await mod.restoreCredsBackupIfCorrupt(dir)).toBe(false);
  });

  it('serializes concurrent saves, the last one wins', async () => {
    const { state, saveCreds } = await mod.useAtomicMultiFileAuthState(dir);
    const saves: Promise<void>[] = [];
    for (const id of [1, 2, 3]) {
      state.creds.registrationId = id;
      saves.push(saveCreds());
    }
    await Promise.all(saves);
    expect((await readJson(credsPath())).registrationId).toBe(3);
  });

  it('flushPendingCredsSave waits for an in-flight save', async () => {
    const { saveCreds } = await mod.useAtomicMultiFileAuthState(dir);
    void saveCreds();
    expect(await mod.flushPendingCredsSave(5000)).toBe(true);
    expect(await readdir(dir)).toContain('creds.json');
  });
});
