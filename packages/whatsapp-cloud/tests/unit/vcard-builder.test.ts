import { describe, it, expect } from 'vitest';
import { buildVCard, validateContactInfo } from '../../src/utils/vcard-builder.js';
import type { ContactInfo } from '../../src/utils/vcard-builder.js';

// ---------------------------------------------------------------------------
// buildVCard
// ---------------------------------------------------------------------------

describe('buildVCard', () => {
  it('should build a basic vCard with required fields only', () => {
    const contact: ContactInfo = { name: 'John Doe', phone: '+5511999999999' };
    const result = buildVCard(contact);

    expect(result).toContain('BEGIN:VCARD');
    expect(result).toContain('VERSION:3.0');
    expect(result).toContain('FN:John Doe');
    expect(result).toContain('TEL;type=CELL;type=VOICE;waid=5511999999999:+5511999999999');
    expect(result).toContain('END:VCARD');
  });

  it('should strip non-digit characters for waid but keep original phone in TEL value', () => {
    const contact: ContactInfo = { name: 'Jane', phone: '+1 (650) 555-1234' };
    const result = buildVCard(contact);

    // waid should have only digits
    expect(result).toContain('waid=16505551234');
    // TEL value should have original phone
    expect(result).toContain(':+1 (650) 555-1234');
  });

  it('should include email when provided', () => {
    const contact: ContactInfo = {
      name: 'Alice',
      phone: '+5511999999999',
      email: 'alice@example.com',
    };
    const result = buildVCard(contact);

    expect(result).toContain('EMAIL:alice@example.com');
  });

  it('should not include EMAIL line when email is not provided', () => {
    const contact: ContactInfo = { name: 'Bob', phone: '+5511999999999' };
    const result = buildVCard(contact);

    expect(result).not.toContain('EMAIL:');
  });

  it('should include organization when provided', () => {
    const contact: ContactInfo = {
      name: 'Carlos',
      phone: '+5511999999999',
      organization: 'Acme Corp',
    };
    const result = buildVCard(contact);

    expect(result).toContain('ORG:Acme Corp');
  });

  it('should not include ORG line when organization is not provided', () => {
    const contact: ContactInfo = { name: 'Dave', phone: '+5511999999999' };
    const result = buildVCard(contact);

    expect(result).not.toContain('ORG:');
  });

  it('should include all optional fields when all are provided', () => {
    const contact: ContactInfo = {
      name: 'Eve',
      phone: '+5511999999999',
      email: 'eve@example.com',
      organization: 'Tech Inc',
    };
    const result = buildVCard(contact);

    expect(result).toContain('FN:Eve');
    expect(result).toContain('EMAIL:eve@example.com');
    expect(result).toContain('ORG:Tech Inc');
    expect(result).toContain('BEGIN:VCARD');
    expect(result).toContain('END:VCARD');
  });

  it('should produce lines separated by newlines', () => {
    const contact: ContactInfo = { name: 'Frank', phone: '5511999999999' };
    const result = buildVCard(contact);
    const lines = result.split('\n');

    expect(lines[0]).toBe('BEGIN:VCARD');
    expect(lines[1]).toBe('VERSION:3.0');
    expect(lines[2]).toBe('FN:Frank');
    expect(lines[lines.length - 1]).toBe('END:VCARD');
  });

  it('should handle phone without + prefix', () => {
    const contact: ContactInfo = { name: 'Grace', phone: '5511999999999' };
    const result = buildVCard(contact);

    expect(result).toContain('waid=5511999999999:5511999999999');
  });
});

// ---------------------------------------------------------------------------
// validateContactInfo
// ---------------------------------------------------------------------------

describe('validateContactInfo', () => {
  it('should return valid for a correct contact', () => {
    const contact: ContactInfo = { name: 'John', phone: '+5511999999999' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(true);
    expect(result.error).toBeUndefined();
  });

  it('should reject empty name', () => {
    const contact: ContactInfo = { name: '', phone: '+5511999999999' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact name is required');
  });

  it('should reject whitespace-only name', () => {
    const contact: ContactInfo = { name: '   ', phone: '+5511999999999' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact name is required');
  });

  it('should reject empty phone', () => {
    const contact: ContactInfo = { name: 'John', phone: '' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact phone is required');
  });

  it('should reject whitespace-only phone', () => {
    const contact: ContactInfo = { name: 'John', phone: '   ' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Contact phone is required');
  });

  it('should reject phone with fewer than 5 digits', () => {
    const contact: ContactInfo = { name: 'John', phone: '1234' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Invalid phone number format');
  });

  it('should accept phone with exactly 5 digits', () => {
    const contact: ContactInfo = { name: 'John', phone: '12345' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(true);
  });

  it('should count only digits for phone validation (ignoring formatting)', () => {
    // "+1 23" has only 3 digits -> should fail
    const contact: ContactInfo = { name: 'John', phone: '+1 23' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Invalid phone number format');
  });

  it('should accept formatted phone with enough digits', () => {
    const contact: ContactInfo = { name: 'John', phone: '+1 (650) 555-1234' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(true);
  });

  it('should reject invalid email (missing @)', () => {
    const contact: ContactInfo = {
      name: 'John',
      phone: '+5511999999999',
      email: 'invalidemail.com',
    };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(false);
    expect(result.error).toBe('Invalid email format');
  });

  it('should accept valid email', () => {
    const contact: ContactInfo = {
      name: 'John',
      phone: '+5511999999999',
      email: 'john@example.com',
    };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(true);
  });

  it('should not validate email if not provided', () => {
    const contact: ContactInfo = { name: 'John', phone: '+5511999999999' };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(true);
  });

  it('should accept contact with all optional fields valid', () => {
    const contact: ContactInfo = {
      name: 'John',
      phone: '+5511999999999',
      email: 'john@test.com',
      organization: 'Test Corp',
    };
    const result = validateContactInfo(contact);

    expect(result.valid).toBe(true);
  });
});
