export interface ContactInfo {
  name: string;
  phone: string;
  email?: string;
  organization?: string;
}

/**
 * Build vCard 3.0 format string from contact information
 *
 * @param contact - Contact information object
 * @returns vCard formatted string
 */
export function buildVCard(contact: ContactInfo): string {
  const lines: string[] = ['BEGIN:VCARD', 'VERSION:3.0', `FN:${contact.name}`];

  // Phone number with WhatsApp ID (waid) if possible
  const phoneDigits = contact.phone.replace(/\D/g, ''); // Remove non-digits
  lines.push(`TEL;type=CELL;type=VOICE;waid=${phoneDigits}:${contact.phone}`);

  // Optional fields
  if (contact.email) {
    lines.push(`EMAIL:${contact.email}`);
  }

  if (contact.organization) {
    lines.push(`ORG:${contact.organization}`);
  }

  lines.push('END:VCARD');

  return lines.join('\n');
}

/**
 * Validate contact information before building vCard
 */
export function validateContactInfo(contact: ContactInfo): { valid: boolean; error?: string } {
  if (!contact.name || contact.name.trim().length === 0) {
    return { valid: false, error: 'Contact name is required' };
  }

  if (!contact.phone || contact.phone.trim().length === 0) {
    return { valid: false, error: 'Contact phone is required' };
  }

  // Basic phone validation (should contain digits)
  const phoneDigits = contact.phone.replace(/\D/g, '');
  if (phoneDigits.length < 5) {
    return { valid: false, error: 'Invalid phone number format' };
  }

  // Email validation if provided
  if (contact.email && !contact.email.includes('@')) {
    return { valid: false, error: 'Invalid email format' };
  }

  return { valid: true };
}
