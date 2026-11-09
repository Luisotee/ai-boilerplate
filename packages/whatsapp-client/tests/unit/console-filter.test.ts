import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  installLibsignalConsoleFilter,
  isLibsignalSessionDump,
} from '../../src/utils/console-filter.js';

describe('isLibsignalSessionDump', () => {
  it.each([
    ['Closing session:', { privKey: 'x' }],
    ['Opening session:', { rootKey: 'x' }],
    ['Removing old closed session:', { chainKey: 'x' }],
    ['Session already closed', { privKey: 'x' }],
  ])('matches %s', (prefix, session) => {
    expect(isLibsignalSessionDump([prefix, session])).toBe(true);
  });

  it.each([
    [['Migrating session to:', 3]],
    [['Session already open']],
    [['something else', { a: 1 }]],
    [[{ msg: 'Closing session:' }]],
    [[]],
  ])('passes %j', (args) => {
    expect(isLibsignalSessionDump(args)).toBe(false);
  });
});

describe('installLibsignalConsoleFilter', () => {
  const originalInfo = console.info;
  const originalWarn = console.warn;
  let info: ReturnType<typeof vi.fn>;
  let warn: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    info = vi.fn();
    warn = vi.fn();
    console.info = info;
    console.warn = warn;
  });

  afterEach(() => {
    console.info = originalInfo;
    console.warn = originalWarn;
  });

  it('drops libsignal session dumps and passes everything else', () => {
    installLibsignalConsoleFilter();

    console.info('Closing session:', { privKey: Buffer.from('secret') });
    console.info('Opening session:', {});
    console.info('Removing old closed session:', {});
    console.warn('Session already closed', {});
    expect(info).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();

    console.info('Migrating session to:', 2);
    console.warn('Closing open session in favor of incoming prekey bundle');
    expect(info).toHaveBeenCalledWith('Migrating session to:', 2);
    expect(warn).toHaveBeenCalledWith('Closing open session in favor of incoming prekey bundle');
  });

  it('is idempotent', () => {
    installLibsignalConsoleFilter();
    const wrapped = console.info;
    installLibsignalConsoleFilter();
    expect(console.info).toBe(wrapped);

    console.info('hello');
    expect(info).toHaveBeenCalledTimes(1);
  });
});
