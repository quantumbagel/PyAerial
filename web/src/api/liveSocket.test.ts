import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { connectLiveSocket, resetLiveSocketForTests, sendWsRequest } from './liveSocket';

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CLOSED = 3;
  readyState = MockWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  receive(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

describe('liveSocket', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    resetLiveSocketForTests();
    vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    resetLiveSocketForTests();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('resolves queued requests after connect', async () => {
    const disconnect = connectLiveSocket({ onMessage: () => {} });
    const promise = sendWsRequest<{ ok: boolean }>('fetchConfig');
    const ws = MockWebSocket.instances[0];
    const req = JSON.parse(ws.sent[0]);
    ws.receive({ type: 'response', id: req.id, success: true, data: { ok: true } });
    await expect(promise).resolves.toEqual({ ok: true });
    disconnect();
  });

  it('rejects pending requests when the socket closes', async () => {
    const disconnect = connectLiveSocket({ onMessage: () => {} });
    const promise = sendWsRequest('fetchFlights', { view: 'live' });
    MockWebSocket.instances[0].close();
    await expect(promise).rejects.toThrow('Connection closed');
    disconnect();
  });

  it('times out stalled requests', async () => {
    vi.useFakeTimers();
    const disconnect = connectLiveSocket({ onMessage: () => {} });
    const promise = sendWsRequest('fetchZones');
    vi.advanceTimersByTime(30_000);
    await expect(promise).rejects.toThrow('Request timed out');
    disconnect();
  });

  it('handles fetchStats requests', async () => {
    const disconnect = connectLiveSocket({ onMessage: () => {} });
    const promise = sendWsRequest('fetchStats', { view: 'live' });
    const ws = MockWebSocket.instances[0];
    const req = JSON.parse(ws.sent[0]);
    ws.receive({
      type: 'response',
      id: req.id,
      success: true,
      data: { live_flights: 5, active_alerts: 2, retained_flights: 10, historical_alerts: 25 },
    });
    await expect(promise).resolves.toEqual({
      live_flights: 5,
      active_alerts: 2,
      retained_flights: 10,
      historical_alerts: 25,
    });
    disconnect();
  });
});
