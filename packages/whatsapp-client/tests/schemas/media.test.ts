import { describe, it, expect } from 'vitest';
import {
  SendLocationSchema,
  SendContactSchema,
  MediaResponseSchema,
  ErrorResponseSchema,
} from '../../src/schemas/media.js';

describe('SendLocationSchema', () => {
  it('should accept valid location with required fields', () => {
    const input = {
      phoneNumber: '5511999999999',
      latitude: -23.5505,
      longitude: -46.6333,
    };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.phoneNumber).toBe('5511999999999');
      expect(result.data.latitude).toBe(-23.5505);
      expect(result.data.longitude).toBe(-46.6333);
      expect(result.data.name).toBeUndefined();
      expect(result.data.address).toBeUndefined();
    }
  });

  it('should accept valid location with optional name and address', () => {
    const input = {
      phoneNumber: '5511999999999',
      latitude: 48.8584,
      longitude: 2.2945,
      name: 'Eiffel Tower',
      address: 'Champ de Mars, 5 Av. Anatole France, 75007 Paris',
    };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.name).toBe('Eiffel Tower');
      expect(result.data.address).toBe('Champ de Mars, 5 Av. Anatole France, 75007 Paris');
    }
  });

  it('should accept latitude at minimum (-90)', () => {
    const input = { phoneNumber: '5511999999999', latitude: -90, longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should accept latitude at maximum (90)', () => {
    const input = { phoneNumber: '5511999999999', latitude: 90, longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should reject latitude below -90', () => {
    const input = { phoneNumber: '5511999999999', latitude: -90.1, longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject latitude above 90', () => {
    const input = { phoneNumber: '5511999999999', latitude: 90.1, longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept longitude at minimum (-180)', () => {
    const input = { phoneNumber: '5511999999999', latitude: 0, longitude: -180 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should accept longitude at maximum (180)', () => {
    const input = { phoneNumber: '5511999999999', latitude: 0, longitude: 180 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should reject longitude below -180', () => {
    const input = { phoneNumber: '5511999999999', latitude: 0, longitude: -180.1 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject longitude above 180', () => {
    const input = { phoneNumber: '5511999999999', latitude: 0, longitude: 180.1 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept zero latitude and longitude (null island)', () => {
    const input = { phoneNumber: '5511999999999', latitude: 0, longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should reject missing phoneNumber', () => {
    const input = { latitude: 0, longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing latitude', () => {
    const input = { phoneNumber: '5511999999999', longitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing longitude', () => {
    const input = { phoneNumber: '5511999999999', latitude: 0 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject string latitude', () => {
    const input = { phoneNumber: '5511999999999', latitude: '48.8584', longitude: 2.2945 };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject string longitude', () => {
    const input = { phoneNumber: '5511999999999', latitude: 48.8584, longitude: '2.2945' };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept decimal precision coordinates', () => {
    const input = {
      phoneNumber: '5511999999999',
      latitude: -23.550520,
      longitude: -46.633308,
    };
    const result = SendLocationSchema.safeParse(input);

    expect(result.success).toBe(true);
  });
});

describe('SendContactSchema', () => {
  it('should accept valid contact with required fields', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John Doe',
      contactPhone: '+5511888888888',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.phoneNumber).toBe('5511999999999');
      expect(result.data.contactName).toBe('John Doe');
      expect(result.data.contactPhone).toBe('+5511888888888');
      expect(result.data.contactEmail).toBeUndefined();
      expect(result.data.contactOrg).toBeUndefined();
    }
  });

  it('should accept valid contact with all optional fields', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'Jane Smith',
      contactPhone: '+1234567890',
      contactEmail: 'jane@example.com',
      contactOrg: 'Acme Corp',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.contactEmail).toBe('jane@example.com');
      expect(result.data.contactOrg).toBe('Acme Corp');
    }
  });

  it('should reject empty contactName', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: '',
      contactPhone: '+5511888888888',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject empty contactPhone', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John Doe',
      contactPhone: '',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing phoneNumber', () => {
    const input = {
      contactName: 'John Doe',
      contactPhone: '+5511888888888',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing contactName', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactPhone: '+5511888888888',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing contactPhone', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John Doe',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject invalid email format', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John Doe',
      contactPhone: '+5511888888888',
      contactEmail: 'invalid-email',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept valid email format', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John Doe',
      contactPhone: '+5511888888888',
      contactEmail: 'john@example.com',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should accept contactName with minimum length of 1', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'J',
      contactPhone: '+5511888888888',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should accept contactPhone with minimum length of 1', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John',
      contactPhone: '1',
    };
    const result = SendContactSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should accept contactOrg as any string', () => {
    const input = {
      phoneNumber: '5511999999999',
      contactName: 'John',
      contactPhone: '+5511888888888',
      contactOrg: '',
    };
    const result = SendContactSchema.safeParse(input);

    // contactOrg is optional with no min constraint, empty string should be accepted
    expect(result.success).toBe(true);
  });
});

describe('Response Schemas', () => {
  describe('MediaResponseSchema', () => {
    it('should accept success with message_id', () => {
      const input = { success: true, message_id: 'BAE5F2B8C1234567' };
      const result = MediaResponseSchema.safeParse(input);

      expect(result.success).toBe(true);
    });

    it('should accept success without message_id', () => {
      const input = { success: true };
      const result = MediaResponseSchema.safeParse(input);

      expect(result.success).toBe(true);
    });

    it('should accept failure response', () => {
      const input = { success: false };
      const result = MediaResponseSchema.safeParse(input);

      expect(result.success).toBe(true);
    });

    it('should reject missing success field', () => {
      const input = { message_id: 'msg_123' };
      const result = MediaResponseSchema.safeParse(input);

      expect(result.success).toBe(false);
    });
  });

  describe('ErrorResponseSchema (media)', () => {
    it('should accept valid error response', () => {
      const result = ErrorResponseSchema.safeParse({ error: 'Not found' });
      expect(result.success).toBe(true);
    });

    it('should reject missing error field', () => {
      const result = ErrorResponseSchema.safeParse({});
      expect(result.success).toBe(false);
    });
  });
});
