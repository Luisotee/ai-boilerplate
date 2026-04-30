import { describe, it, expect, beforeEach } from 'vitest';
import {
  metricsRegistry,
  messagesReceived,
  messagesSent,
  apiPollDuration,
} from '../../src/routes/metrics.js';

describe('metrics registry', () => {
  beforeEach(() => {
    metricsRegistry.resetMetrics();
  });

  it('exposes Prometheus exposition format with labeled counters', async () => {
    messagesReceived.inc({ client: 'baileys', type: 'text', conversation_type: 'private' });
    messagesReceived.inc({ client: 'baileys', type: 'image', conversation_type: 'group' }, 2);
    messagesSent.inc({ client: 'baileys', type: 'text' });
    messagesSent.inc({ client: 'baileys', type: 'audio' }, 3);

    const output = await metricsRegistry.metrics();

    expect(output).toContain('# HELP chat_messages_received_total');
    expect(output).toContain('# TYPE chat_messages_received_total counter');
    expect(output).toContain(
      'chat_messages_received_total{client="baileys",type="text",conversation_type="private"} 1'
    );
    expect(output).toContain(
      'chat_messages_received_total{client="baileys",type="image",conversation_type="group"} 2'
    );
    expect(output).toContain('chat_messages_sent_total{client="baileys",type="text"} 1');
    expect(output).toContain('chat_messages_sent_total{client="baileys",type="audio"} 3');
  });

  it('exposes ai_api_poll_duration_seconds histogram', async () => {
    const end = apiPollDuration.startTimer();
    end({ status: 'success' });

    const output = await metricsRegistry.metrics();
    expect(output).toContain('# TYPE ai_api_poll_duration_seconds histogram');
    expect(output).toContain('ai_api_poll_duration_seconds_bucket');
    expect(output).toContain('status="success"');
  });

  it('includes default Node process metrics', async () => {
    const output = await metricsRegistry.metrics();
    expect(output).toContain('process_cpu_user_seconds_total');
    expect(output).toContain('nodejs_heap_size_total_bytes');
  });

  it('uses the Prometheus text content type', () => {
    expect(metricsRegistry.contentType).toMatch(/^text\/plain/);
    expect(metricsRegistry.contentType).toContain('version=0.0.4');
  });
});
