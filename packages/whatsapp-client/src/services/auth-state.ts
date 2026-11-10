/**
 * Baileys multi-file auth state with crash-safe `creds.json` writes.
 *
 * rc13's `useMultiFileAuthState().saveCreds` is a plain `writeFile`: a SIGKILL
 * or a power cut mid-write leaves a truncated `creds.json`, which Baileys then
 * reads as "no creds" and answers with a fresh pairing QR, i.e. a re-pair.
 * Here creds are written to a temp file, fsynced and renamed over the old one
 * (atomic on POSIX), with the previous good copy kept as `creds.json.bak` and
 * restored at startup if `creds.json` doesn't parse. Signal keys (the other
 * files) still go through Baileys' own writer.
 */
import {
  BufferJSON,
  useMultiFileAuthState,
  type AuthenticationState,
} from '@whiskeysockets/baileys';
import { access, copyFile, open, readFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
import { logger } from '../logger.js';

const CREDS_FILE = 'creds.json';

// Tail of the creds write chain: one write at a time, in order. Never rejects.
let pendingSave: Promise<void> = Promise.resolve();

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

async function parsesAsJson(path: string): Promise<boolean> {
  try {
    JSON.parse(await readFile(path, 'utf-8'));
    return true;
  } catch {
    return false;
  }
}

/** If `creds.json` exists but doesn't parse, restore the last good backup. */
export async function restoreCredsBackupIfCorrupt(dir: string): Promise<boolean> {
  const credsPath = join(dir, CREDS_FILE);
  const backupPath = `${credsPath}.bak`;
  if (!(await exists(credsPath)) || (await parsesAsJson(credsPath))) return false;
  if (!(await parsesAsJson(backupPath))) {
    logger.error('creds.json is corrupt and there is no usable backup — a re-pair is needed');
    return false;
  }
  await copyFile(backupPath, credsPath);
  logger.warn('creds.json was corrupt — restored it from creds.json.bak');
  return true;
}

/** Write creds via temp file + fsync + rename, keeping the previous good copy as `.bak`. */
export async function writeCredsAtomically(dir: string, creds: unknown): Promise<void> {
  const credsPath = join(dir, CREDS_FILE);
  const tmpPath = `${credsPath}.tmp`;
  if (await parsesAsJson(credsPath)) {
    await copyFile(credsPath, `${credsPath}.bak`);
  }
  const handle = await open(tmpPath, 'w');
  try {
    await handle.writeFile(JSON.stringify(creds, BufferJSON.replacer));
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(tmpPath, credsPath);
}

/** `useMultiFileAuthState` with an atomic, serialized `saveCreds`. */
export async function useAtomicMultiFileAuthState(
  dir: string
): Promise<{ state: AuthenticationState; saveCreds: () => Promise<void> }> {
  await restoreCredsBackupIfCorrupt(dir);
  const { state } = await useMultiFileAuthState(dir);
  const saveCreds = (): Promise<void> => {
    const run = pendingSave.then(() => writeCredsAtomically(dir, state.creds));
    pendingSave = run.catch(() => undefined);
    return run;
  };
  return { state, saveCreds };
}

/** Wait for the in-flight creds write(s), at most `timeoutMs`. True when they finished. */
export async function flushPendingCredsSave(timeoutMs: number): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<false>((resolve) => {
    timer = setTimeout(() => resolve(false), timeoutMs);
  });
  try {
    return await Promise.race([pendingSave.then(() => true as const), timeout]);
  } finally {
    clearTimeout(timer);
  }
}
