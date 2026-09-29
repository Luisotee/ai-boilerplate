import { describe, it, expect } from 'vitest';
import { GraphApiError } from '../../src/errors/GraphApiError.js';
import { RequestTimeoutError } from '../../src/errors/RequestTimeoutError.js';
import { sendErrorResponse } from '../../src/utils/send-error.js';

describe('sendErrorResponse', () => {
  it.each([
    [new GraphApiError(429, ''), 429],
    [new GraphApiError(400, ''), 422],
    [new GraphApiError(404, ''), 422],
    [new GraphApiError(500, ''), 502],
    [new GraphApiError(503, ''), 502],
    [new RequestTimeoutError('https://graph', 1000), 502],
    [new Error('network'), 502],
    ['not even an error', 502],
  ])('%o -> %i', (err, status) => {
    expect(sendErrorResponse(err).statusCode).toBe(status);
  });

  it('never echoes the Graph body', () => {
    const { error } = sendErrorResponse(new GraphApiError(400, 'recipient +5511999999999'));
    expect(error).not.toContain('5511');
  });
});
