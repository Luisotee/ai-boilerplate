import { describe, it, expect, beforeEach } from 'vitest';
import {
  metricsRegistry,
  messagesReceived,
  messagesSent,
  apiPollDuration,
} from '../../src/routes/metrics.js';

describe('metrics registry (cloud)', () => {
  beforeEach(() => {
    metricsRegistry.resetMetrics();
  });

  it('exposes Prometheus exposition format with labeled counters', async () => {
    messagesReceived.inc({ client: 'cloud', type: 'text', conversation_type: 'private' });
    messagesReceived.inc({ client: 'cloud', type: 'image', conversation_type: 'private' }, 2);
    messagesSent.inc({ client: 'cloud', type: 'text' });
    messagesSent.inc({ client: 'cloud', type: 'audio' }, 3);

    const output = await metricsRegistry.metrics();

    expect(output).toContain('# HELP chat_messages_received_total');
    expect(output).toContain(
      'chat_messages_received_total{client="cloud",type="text",conversation_type="private"} 1'
    );
    expect(output).toContain(
      'chat_messages_received_total{client="cloud",type="image",conversation_type="private"} 2'
    );
    expect(output).toContain('chat_messages_sent_total{client="cloud",type="text"} 1');
    expect(output).toContain('chat_messages_sent_total{client="cloud",type="audio"} 3');
  });

  it('exposes ai_api_poll_duration_seconds histogram', async () => {
    const end = apiPollDuration.startTimer();
    end({ status: 'error' });

    const output = await metricsRegistry.metrics();
    expect(output).toContain('# TYPE ai_api_poll_duration_seconds histogram');
    expect(output).toContain('status="error"');
  });

  it('uses the Prometheus text content type', () => {
    expect(metricsRegistry.contentType).toContain('version=0.0.4');
  });
});
