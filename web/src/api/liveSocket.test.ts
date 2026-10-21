import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class FakeSocket {
  static instances: FakeSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  message(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }
  closeWith(code: number, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }
}

beforeEach(() => {
  vi.resetModules();
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('liveSocket', () => {
  it('holds requests until the hello, then sends them', async () => {
    const live = await import('./liveSocket');
    const onOpen = vi.fn();
    live.connectLiveSocket({ onOpen });
    const result = live.sendWsRequest('fetchStats');
    const ws = FakeSocket.instances[0];
    ws.open();
    expect(ws.sent).toHaveLength(0);
    expect(onOpen).not.toHaveBeenCalled();

    ws.message({ type: 'hello', protocol: 'pyaerial', version: 1, streams: [], actions: [] });
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(ws.sent).toHaveLength(1);
    const { id } = JSON.parse(ws.sent[0]);
    ws.message({ type: 'response', id, success: true, data: { live_flights: 1 } });
    await expect(result).resolves.toEqual({ live_flights: 1 });
  });

  it('fails queued requests as rejected when the origin is refused (1008)', async () => {
    const live = await import('./liveSocket');
    const result = live.sendWsRequest('fetchStats');
    const ws = FakeSocket.instances[0];
    ws.open(); // the server accepts, then closes
    ws.closeWith(1008, 'origin not allowed');
    await expect(result).rejects.toMatchObject({ code: 'rejected' });
    await expect(live.sendWsRequest('fetchZones')).rejects.toMatchObject({ code: 'rejected' });
  });

  it('fails queued requests as busy at the connection cap (1013) and keeps retrying', async () => {
    vi.useFakeTimers();
    const live = await import('./liveSocket');
    const result = live.sendWsRequest('fetchStats');
    FakeSocket.instances[0].open();
    FakeSocket.instances[0].closeWith(1013, 'too many connections');
    await expect(result).rejects.toMatchObject({ code: 'busy' });
    await vi.advanceTimersByTimeAsync(9000);
    expect(FakeSocket.instances.length).toBe(2);
  });

  it('reports a server error code on failed responses', async () => {
    const live = await import('./liveSocket');
    live.connectLiveSocket({});
    const ws = FakeSocket.instances[0];
    ws.open();
    ws.message({ type: 'hello' });
    const result = live.sendWsRequest('fetchFlights');
    const { id } = JSON.parse(ws.sent[0]);
    ws.message({ type: 'response', id, success: false, error: 'slow down', code: 'rate_limited' });
    await expect(result).rejects.toMatchObject({ code: 'rate_limited' });
  });
});
