import { describe, it, expect } from 'vitest';
import {
  buildVCard,
  validateContactInfo,
  type ContactInfo,
} from '../../src/utils/vcard-builder.js';

describe('buildVCard', () => {
  it('should build a minimal vCard with name and phone', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+5511999999999',
    };

    const vcard = buildVCard(contact);

    expect(vcard).toContain('BEGIN:VCARD');
    expect(vcard).toContain('VERSION:3.0');
    expect(vcard).toContain('FN:John Doe');
    expect(vcard).toContain('TEL;type=CELL;type=VOICE;waid=5511999999999:+5511999999999');
    expect(vcard).toContain('END:VCARD');
    expect(vcard).not.toContain('EMAIL:');
    expect(vcard).not.toContain('ORG:');
  });

  it('should include email when provided', () => {
    const contact: ContactInfo = {
      name: 'Jane Smith',
      phone: '+1234567890',
      email: 'jane@example.com',
    };

    const vcard = buildVCard(contact);

    expect(vcard).toContain('EMAIL:jane@example.com');
  });

  it('should include organization when provided', () => {
    const contact: ContactInfo = {
      name: 'Bob Builder',
      phone: '+5511888888888',
      organization: 'Acme Corp',
    };

    const vcard = buildVCard(contact);

    expect(vcard).toContain('ORG:Acme Corp');
  });

  it('should include both email and organization when provided', () => {
    const contact: ContactInfo = {
      name: 'Alice Wonder',
      phone: '+5511777777777',
      email: 'alice@wonderland.com',
      organization: 'Wonderland Inc',
    };

    const vcard = buildVCard(contact);

    expect(vcard).toContain('EMAIL:alice@wonderland.com');
    expect(vcard).toContain('ORG:Wonderland Inc');
  });

  it('should strip non-digit characters from phone for waid field', () => {
    const contact: ContactInfo = {
      name: 'Charlie',
      phone: '+55 (11) 99999-9999',
    };

    const vcard = buildVCard(contact);

    // waid should contain only digits
    expect(vcard).toContain('waid=5511999999999');
    // The full TEL line should preserve the original phone format
    expect(vcard).toContain(':+55 (11) 99999-9999');
  });

  it('should produce lines separated by newlines', () => {
    const contact: ContactInfo = {
      name: 'Test User',
      phone: '1234567890',
    };

    const vcard = buildVCard(contact);
    const lines = vcard.split('\n');

    expect(lines[0]).toBe('BEGIN:VCARD');
    expect(lines[1]).toBe('VERSION:3.0');
    expect(lines[2]).toBe('FN:Test User');
    expect(lines[lines.length - 1]).toBe('END:VCARD');
  });

  it('should place fields in the correct order', () => {
    const contact: ContactInfo = {
      name: 'Order Test',
      phone: '+1234567890',
      email: 'order@test.com',
      organization: 'Test Org',
    };

    const vcard = buildVCard(contact);
    const lines = vcard.split('\n');

    expect(lines[0]).toBe('BEGIN:VCARD');
    expect(lines[1]).toBe('VERSION:3.0');
    expect(lines[2]).toBe('FN:Order Test');
    expect(lines[3]).toContain('TEL;type=CELL;type=VOICE;waid=');
    expect(lines[4]).toBe('EMAIL:order@test.com');
    expect(lines[5]).toBe('ORG:Test Org');
    expect(lines[6]).toBe('END:VCARD');
  });

  it('should handle phone number with only digits', () => {
    const contact: ContactInfo = {
      name: 'Digits Only',
      phone: '5511999999999',
    };

    const vcard = buildVCard(contact);

    expect(vcard).toContain('waid=5511999999999:5511999999999');
  });

  it('should handle empty optional fields (undefined)', () => {
    const contact: ContactInfo = {
      name: 'Minimal',
      phone: '12345',
      email: undefined,
      organization: undefined,
    };

    const vcard = buildVCard(contact);
    const lines = vcard.split('\n');

    // Should only have: BEGIN, VERSION, FN, TEL, END
    expect(lines).toHaveLength(5);
  });
});

describe('validateContactInfo', () => {
  it('should return valid for a correct contact', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+5511999999999',
    };

    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });

  it('should return valid for contact with all optional fields', () => {
    const contact: ContactInfo = {
      name: 'Jane Smith',
      phone: '+5511888888888',
      email: 'jane@example.com',
      organization: 'Acme Corp',
    };

    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });

  it('should reject empty name', () => {
    const contact: ContactInfo = {
      name: '',
      phone: '+5511999999999',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact name is required');
  });

  it('should reject whitespace-only name', () => {
    const contact: ContactInfo = {
      name: '   ',
      phone: '+5511999999999',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact name is required');
  });

  it('should reject empty phone', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact phone is required');
  });

  it('should reject whitespace-only phone', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '   ',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact phone is required');
  });

  it('should reject phone with fewer than 5 digits', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '1234',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Invalid phone number format');
  });

  it('should accept phone with exactly 5 digits', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '12345',
    };

    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });

  it('should count only digits in phone for validation', () => {
    // "+1-2-3-4" has only 4 digits -> invalid
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+1-2-3-4',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Invalid phone number format');
  });

  it('should accept phone with enough digits even if formatted', () => {
    // "+1 (234) 5" has 5 digits -> valid
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+1 (234) 5',
    };

    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });

  it('should reject invalid email without @', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+5511999999999',
      email: 'invalid-email',
    };

    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Invalid email format');
  });

  it('should accept email with @', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+5511999999999',
      email: 'user@domain',
    };

    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });

  it('should skip email validation when email is not provided', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+5511999999999',
    };

    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });

  it('should not validate organization (optional, no rules)', () => {
    const contact: ContactInfo = {
      name: 'John Doe',
      phone: '+5511999999999',
      organization: '',
    };

    // Organization with empty string should still be valid
    // (buildVCard uses truthiness check, but validateContactInfo doesn't validate org)
    const result = validateContactInfo(contact);

    expect(result).toEqual({ valid: true });
  });
});
