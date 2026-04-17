import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  stripDeviceSuffix,
  isGroupChat,
  extractPhoneFromJid,
  phoneFromJid,
  isLid,
  isJid,
} from '../../src/utils/jid.js';

vi.mock('../../src/services/baileys.js', () => ({
  getBaileysSocket: vi.fn(),
}));

describe('stripDeviceSuffix', () => {
  it('should strip device suffix from JID', () => {
    expect(stripDeviceSuffix('5491126726818:50@s.whatsapp.net')).toBe(
      '5491126726818@s.whatsapp.net'
    );
  });

  it('should strip device suffix with different device numbers', () => {
    expect(stripDeviceSuffix('5491126726818:0@s.whatsapp.net')).toBe(
      '5491126726818@s.whatsapp.net'
    );
    expect(stripDeviceSuffix('5491126726818:1@s.whatsapp.net')).toBe(
      '5491126726818@s.whatsapp.net'
    );
    expect(stripDeviceSuffix('5491126726818:99@s.whatsapp.net')).toBe(
      '5491126726818@s.whatsapp.net'
    );
  });

  it('should return JID unchanged if no device suffix', () => {
    expect(stripDeviceSuffix('5491126726818@s.whatsapp.net')).toBe(
      '5491126726818@s.whatsapp.net'
    );
  });

  it('should handle group JIDs (no device suffix expected)', () => {
    expect(stripDeviceSuffix('120363123456789@g.us')).toBe('120363123456789@g.us');
  });

  it('should handle LID JIDs', () => {
    expect(stripDeviceSuffix('123456789:5@lid')).toBe('123456789@lid');
  });

  it('should handle JID with multi-digit device number', () => {
    expect(stripDeviceSuffix('1234567890:123@s.whatsapp.net')).toBe(
      '1234567890@s.whatsapp.net'
    );
  });
});

describe('isGroupChat', () => {
  it('should return true for group JIDs', () => {
    expect(isGroupChat('120363123456789@g.us')).toBe(true);
  });

  it('should return false for individual JIDs', () => {
    expect(isGroupChat('5491126726818@s.whatsapp.net')).toBe(false);
  });

  it('should return false for LID JIDs', () => {
    expect(isGroupChat('123456789@lid')).toBe(false);
  });

  it('should return false for strings not ending with @g.us', () => {
    expect(isGroupChat('test@example.com')).toBe(false);
  });

  it('should return false for empty string', () => {
    expect(isGroupChat('')).toBe(false);
  });

  it('should return false for string containing @g.us but not ending with it', () => {
    expect(isGroupChat('120363@g.us.extra')).toBe(false);
  });
});

describe('extractPhoneFromJid', () => {
  it('should extract phone from individual JID', () => {
    expect(extractPhoneFromJid('5491126726818@s.whatsapp.net')).toBe('5491126726818');
  });

  it('should extract group ID from group JID', () => {
    expect(extractPhoneFromJid('120363123456789@g.us')).toBe('120363123456789');
  });

  it('should extract identifier from LID JID', () => {
    expect(extractPhoneFromJid('123456789@lid')).toBe('123456789');
  });

  it('should handle JID with device suffix (returns phone:device)', () => {
    expect(extractPhoneFromJid('5491126726818:50@s.whatsapp.net')).toBe('5491126726818:50');
  });

  it('should return the full string if no @ is present', () => {
    expect(extractPhoneFromJid('5491126726818')).toBe('5491126726818');
  });

  it('should handle empty string', () => {
    expect(extractPhoneFromJid('')).toBe('');
  });
});

describe('phoneFromJid', () => {
  it('should return E.164 phone from individual JID', () => {
    expect(phoneFromJid('5491126726818@s.whatsapp.net')).toBe('+5491126726818');
  });

  it('should return null for group JIDs', () => {
    expect(phoneFromJid('120363123456789@g.us')).toBeNull();
  });

  it('should return null for LID JIDs', () => {
    expect(phoneFromJid('123456789@lid')).toBeNull();
  });

  it('should return null for other JID formats', () => {
    expect(phoneFromJid('user@example.com')).toBeNull();
  });

  it('should prepend + to phone number', () => {
    const result = phoneFromJid('1234567890@s.whatsapp.net');
    expect(result).toBe('+1234567890');
    expect(result!.startsWith('+')).toBe(true);
  });

  it('should handle JID with device suffix', () => {
    // Note: the device suffix is part of the "phone" portion before @
    // "5491126726818:50@s.whatsapp.net" -> "+5491126726818:50"
    const result = phoneFromJid('5491126726818:50@s.whatsapp.net');
    expect(result).toBe('+5491126726818:50');
  });
});

describe('isLid', () => {
  it('should return true for LID JIDs', () => {
    expect(isLid('123456789@lid')).toBe(true);
  });

  it('should return false for individual JIDs', () => {
    expect(isLid('5491126726818@s.whatsapp.net')).toBe(false);
  });

  it('should return false for group JIDs', () => {
    expect(isLid('120363123456789@g.us')).toBe(false);
  });

  it('should return false for empty string', () => {
    expect(isLid('')).toBe(false);
  });

  it('should return false for strings containing @lid but not ending with it', () => {
    expect(isLid('123@lid.extra')).toBe(false);
  });
});

describe('isJid', () => {
  it('should return true for individual JIDs', () => {
    expect(isJid('5491126726818@s.whatsapp.net')).toBe(true);
  });

  it('should return true for group JIDs', () => {
    expect(isJid('120363123456789@g.us')).toBe(true);
  });

  it('should return true for LID JIDs', () => {
    expect(isJid('123456789@lid')).toBe(true);
  });

  it('should return true for any string containing @', () => {
    expect(isJid('user@domain')).toBe(true);
  });

  it('should return false for plain phone numbers', () => {
    expect(isJid('5491126726818')).toBe(false);
  });

  it('should return false for empty string', () => {
    expect(isJid('')).toBe(false);
  });

  it('should return false for strings without @', () => {
    expect(isJid('nophonehere')).toBe(false);
  });

  it('should return true for @ at the start', () => {
    expect(isJid('@s.whatsapp.net')).toBe(true);
  });

  it('should return true for @ at the end', () => {
    expect(isJid('user@')).toBe(true);
  });
});

describe('normalizeJid', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('should return input unchanged when already a JID', async () => {
    const { normalizeJid } = await import('../../src/utils/jid.js');
    const { getBaileysSocket } = await import('../../src/services/baileys.js');

    const result = await normalizeJid('5491126726818@s.whatsapp.net');

    expect(result).toBe('5491126726818@s.whatsapp.net');
    expect(getBaileysSocket).not.toHaveBeenCalled();
  });

  it('should return jid from onWhatsApp when phone exists', async () => {
    const onWhatsApp = vi
      .fn()
      .mockResolvedValue([{ exists: true, jid: '5491126726818@s.whatsapp.net' }]);
    const { getBaileysSocket } = await import('../../src/services/baileys.js');
    vi.mocked(getBaileysSocket).mockReturnValue({ onWhatsApp } as never);

    const { normalizeJid } = await import('../../src/utils/jid.js');
    const result = await normalizeJid('5491126726818');

    expect(result).toBe('5491126726818@s.whatsapp.net');
    expect(onWhatsApp).toHaveBeenCalledWith('5491126726818');
  });

  it('should throw "not registered" when exists is false', async () => {
    const onWhatsApp = vi.fn().mockResolvedValue([{ exists: false }]);
    const { getBaileysSocket } = await import('../../src/services/baileys.js');
    vi.mocked(getBaileysSocket).mockReturnValue({ onWhatsApp } as never);

    const { normalizeJid } = await import('../../src/utils/jid.js');

    await expect(normalizeJid('5491126726818')).rejects.toThrow(
      /is not registered on WhatsApp/
    );
  });

  it('should throw "not registered" when onWhatsApp returns empty array', async () => {
    const onWhatsApp = vi.fn().mockResolvedValue([]);
    const { getBaileysSocket } = await import('../../src/services/baileys.js');
    vi.mocked(getBaileysSocket).mockReturnValue({ onWhatsApp } as never);

    const { normalizeJid } = await import('../../src/utils/jid.js');

    await expect(normalizeJid('5491126726818')).rejects.toThrow(
      /is not registered on WhatsApp/
    );
  });

  // Regression: Baileys onWhatsApp can return undefined (socket disconnected /
  // request timed out). Previously this crashed with "undefined is not iterable";
  // then the null-guard silently mapped it to "not registered", masking socket
  // failures as invalid numbers. It must now surface as a distinct lookup error.
  it('should throw lookup-failed error when onWhatsApp returns undefined', async () => {
    const onWhatsApp = vi.fn().mockResolvedValue(undefined);
    const { getBaileysSocket } = await import('../../src/services/baileys.js');
    vi.mocked(getBaileysSocket).mockReturnValue({ onWhatsApp } as never);

    const { normalizeJid } = await import('../../src/utils/jid.js');

    await expect(normalizeJid('5491126726818')).rejects.toThrow(/WhatsApp lookup failed/);
    await expect(normalizeJid('5491126726818')).rejects.not.toThrow(
      /is not registered on WhatsApp/
    );
  });
});
