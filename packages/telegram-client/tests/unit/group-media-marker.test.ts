/**
 * Unit tests for the [Image] / [Document] save-only markers used in
 * non-mentioned group photos and documents. These mirror the markers
 * produced by the Baileys client (whatsapp-client/src/whatsapp.ts:226-273)
 * so the AI sees consistent group history across both platforms.
 */

import { describe, it, expect } from 'vitest';
import { documentMarker, imageMarker } from '../../src/utils/group-media-marker.js';

describe('imageMarker', () => {
  it('returns "[Image]" with no caption', () => {
    expect(imageMarker(null)).toBe('[Image]');
    expect(imageMarker(undefined)).toBe('[Image]');
    expect(imageMarker('')).toBe('[Image]');
  });

  it('embeds the caption when present', () => {
    expect(imageMarker('look at this')).toBe('[Image: look at this]');
  });
});

describe('documentMarker', () => {
  it('returns "[Document: filename]" with no caption', () => {
    expect(documentMarker('report.pdf', null)).toBe('[Document: report.pdf]');
    expect(documentMarker('report.pdf', undefined)).toBe('[Document: report.pdf]');
    expect(documentMarker('report.pdf', '')).toBe('[Document: report.pdf]');
  });

  it('appends caption with " - " separator (matches Baileys formatting)', () => {
    expect(documentMarker('report.pdf', 'see page 4')).toBe('[Document: report.pdf] - see page 4');
  });
});
