import { describe, it, expect, vi } from 'vitest';

// Mock the logger module to prevent config/pino initialization errors
vi.mock('../../src/logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

import {
  getSenderName,
  isBotMentioned,
  isReplyToBotMessage,
  shouldRespondInGroup,
} from '../../src/utils/message.js';

// Helper to create minimal WAMessage-like objects
function createMessage(overrides: Record<string, any> = {}): any {
  return {
    key: {
      remoteJid: '5511999999999@s.whatsapp.net',
      participant: undefined,
      ...overrides.key,
    },
    message: overrides.message ?? null,
    pushName: overrides.pushName ?? undefined,
    verifiedBizName: overrides.verifiedBizName ?? undefined,
  };
}

describe('getSenderName', () => {
  it('should return pushName when available', () => {
    const msg = createMessage({ pushName: 'John Doe' });

    expect(getSenderName(msg)).toBe('John Doe');
  });

  it('should return verifiedBizName when pushName is not available', () => {
    const msg = createMessage({
      pushName: undefined,
      verifiedBizName: 'Business Inc',
    });

    expect(getSenderName(msg)).toBe('Business Inc');
  });

  it('should prefer pushName over verifiedBizName', () => {
    const msg = createMessage({
      pushName: 'John',
      verifiedBizName: 'Business',
    });

    expect(getSenderName(msg)).toBe('John');
  });

  it('should fall back to extracting phone from participant JID', () => {
    const msg = createMessage({
      pushName: undefined,
      verifiedBizName: undefined,
      key: {
        remoteJid: '120363123456789@g.us',
        participant: '5511888888888@s.whatsapp.net',
      },
    });

    expect(getSenderName(msg)).toBe('5511888888888');
  });

  it('should fall back to extracting phone from remoteJid when no participant', () => {
    const msg = createMessage({
      pushName: undefined,
      verifiedBizName: undefined,
      key: {
        remoteJid: '5511999999999@s.whatsapp.net',
        participant: undefined,
      },
    });

    expect(getSenderName(msg)).toBe('5511999999999');
  });

  it('should use empty string pushName as truthy fallback', () => {
    // Empty string is falsy in JS, so it falls through to verifiedBizName
    const msg = createMessage({
      pushName: '',
      verifiedBizName: 'Business',
    });

    expect(getSenderName(msg)).toBe('Business');
  });
});

describe('isBotMentioned', () => {
  const botJid = '5511000000000@s.whatsapp.net';
  const botLid = '123456789@lid';

  it('should return true when bot JID is in mentionedJid', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botJid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should return true when bot LID is in mentionedJid', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botLid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid, botLid)).toBe(true);
  });

  it('should return false when bot is not mentioned', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: ['5511111111111@s.whatsapp.net'],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(false);
  });

  it('should return false when no contextInfo exists', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {},
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(false);
  });

  it('should return false when message is null', () => {
    const msg = createMessage({ message: null });

    expect(isBotMentioned(msg, botJid)).toBe(false);
  });

  it('should return false when mentionedJid is empty', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(false);
  });

  it('should return false for LID match when botLid is not provided', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botLid],
          },
        },
      },
    });

    // botLid not passed (undefined), so even if LID is in mentions, it should not match
    expect(isBotMentioned(msg, botJid)).toBe(false);
  });

  it('should detect mention in imageMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        imageMessage: {
          contextInfo: {
            mentionedJid: [botJid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should detect mention in audioMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        audioMessage: {
          contextInfo: {
            mentionedJid: [botJid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should detect mention in videoMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        videoMessage: {
          contextInfo: {
            mentionedJid: [botJid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should detect mention in documentMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        documentMessage: {
          contextInfo: {
            mentionedJid: [botJid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should detect mention in documentWithCaptionMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        documentWithCaptionMessage: {
          message: {
            documentMessage: {
              contextInfo: {
                mentionedJid: [botJid],
              },
            },
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should detect mention in viewOnceMessage imageMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        viewOnceMessage: {
          message: {
            imageMessage: {
              contextInfo: {
                mentionedJid: [botJid],
              },
            },
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should detect mention in viewOnceMessage videoMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        viewOnceMessage: {
          message: {
            videoMessage: {
              contextInfo: {
                mentionedJid: [botJid],
              },
            },
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });

  it('should return true when both JID and LID are mentioned', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botJid, botLid],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid, botLid)).toBe(true);
  });

  it('should handle multiple mentions including bot', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [
              '5511111111111@s.whatsapp.net',
              botJid,
              '5511222222222@s.whatsapp.net',
            ],
          },
        },
      },
    });

    expect(isBotMentioned(msg, botJid)).toBe(true);
  });
});

describe('isReplyToBotMessage', () => {
  const botJid = '5511000000000@s.whatsapp.net';
  const botLid = '123456789@lid';

  it('should return true when quoted participant matches botJid', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: botJid,
          },
        },
      },
    });

    expect(isReplyToBotMessage(msg, botJid)).toBe(true);
  });

  it('should return true when quoted participant matches botLid', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: botLid,
          },
        },
      },
    });

    expect(isReplyToBotMessage(msg, botJid, botLid)).toBe(true);
  });

  it('should return false when quoted participant is a different user', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: '5511111111111@s.whatsapp.net',
          },
        },
      },
    });

    expect(isReplyToBotMessage(msg, botJid)).toBe(false);
  });

  it('should return false when no contextInfo exists', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {},
      },
    });

    expect(isReplyToBotMessage(msg, botJid)).toBe(false);
  });

  it('should return false when message is null', () => {
    const msg = createMessage({ message: null });

    expect(isReplyToBotMessage(msg, botJid)).toBe(false);
  });

  it('should return false for LID match when botLid is not provided', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: botLid,
          },
        },
      },
    });

    expect(isReplyToBotMessage(msg, botJid)).toBe(false);
  });

  it('should detect reply in imageMessage contextInfo', () => {
    const msg = createMessage({
      message: {
        imageMessage: {
          contextInfo: {
            participant: botJid,
          },
        },
      },
    });

    expect(isReplyToBotMessage(msg, botJid)).toBe(true);
  });

  it('should return false when participant is undefined', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: undefined,
          },
        },
      },
    });

    expect(isReplyToBotMessage(msg, botJid)).toBe(false);
  });
});

describe('shouldRespondInGroup', () => {
  const botJid = '5511000000000@s.whatsapp.net';
  const botLid = '123456789@lid';

  it('should return true when bot is mentioned', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botJid],
          },
        },
      },
    });

    expect(shouldRespondInGroup(msg, botJid)).toBe(true);
  });

  it('should return true when message is a reply to bot', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: botJid,
          },
        },
      },
    });

    expect(shouldRespondInGroup(msg, botJid)).toBe(true);
  });

  it('should return true when bot is mentioned via LID', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botLid],
          },
        },
      },
    });

    expect(shouldRespondInGroup(msg, botJid, botLid)).toBe(true);
  });

  it('should return true when message is a reply to bot via LID', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            participant: botLid,
          },
        },
      },
    });

    expect(shouldRespondInGroup(msg, botJid, botLid)).toBe(true);
  });

  it('should return false when bot is neither mentioned nor replied to', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: ['5511111111111@s.whatsapp.net'],
            participant: '5511222222222@s.whatsapp.net',
          },
        },
      },
    });

    expect(shouldRespondInGroup(msg, botJid, botLid)).toBe(false);
  });

  it('should return false when no message content exists', () => {
    const msg = createMessage({ message: null });

    expect(shouldRespondInGroup(msg, botJid)).toBe(false);
  });

  it('should return true when both mentioned and replied to', () => {
    const msg = createMessage({
      message: {
        extendedTextMessage: {
          contextInfo: {
            mentionedJid: [botJid],
            participant: botJid,
          },
        },
      },
    });

    expect(shouldRespondInGroup(msg, botJid)).toBe(true);
  });
});
