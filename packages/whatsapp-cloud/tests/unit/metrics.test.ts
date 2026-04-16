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
    messagesReceived.inc({ type: 'text', conversation_type: 'private' });
    messagesReceived.inc({ type: 'image', conversation_type: 'private' }, 2);
    messagesSent.inc({ type: 'text' });
    messagesSent.inc({ type: 'audio' }, 3);

    const output = await metricsRegistry.metrics();

    expect(output).toContain('# HELP whatsapp_messages_received_total');
    expect(output).toContain(
      'whatsapp_messages_received_total{type="text",conversation_type="private"} 1'
    );
    expect(output).toContain(
      'whatsapp_messages_received_total{type="image",conversation_type="private"} 2'
    );
    expect(output).toContain('whatsapp_messages_sent_total{type="text"} 1');
    expect(output).toContain('whatsapp_messages_sent_total{type="audio"} 3');
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
