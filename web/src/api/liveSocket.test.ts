import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState: number = WebSocket.CONNECTING;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = WebSocket.CLOSED;
  }

  serverOpen() {
    this.readyState = WebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  serverMessage(data: string) {
    this.onmessage?.(new MessageEvent('message', { data }));
  }

  serverClose(code = 1006, reason = '') {
    this.readyState = WebSocket.CLOSED;
    this.onclose?.({ code, reason } as CloseEvent);
  }
}

describe('liveSocket', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('rejects pending and queued requests with code rejected after 1008 without hello', async () => {
    const { connectLiveSocket, sendWsRequest } = await import('./liveSocket');
    const onOpen = vi.fn();
    connectLiveSocket({ onOpen });

    const first = sendWsRequest('list_flights', {});
    const second = sendWsRequest('list_flights', {});
    const ws = FakeSocket.instances[0];
    ws.serverOpen();
    expect(onOpen).not.toHaveBeenCalled();
    expect(ws.sent).toHaveLength(0);

    ws.serverClose(1008, 'Origin not allowed');
    await expect(first).rejects.toMatchObject({ code: 'rejected' });
    await expect(second).rejects.toMatchObject({ code: 'rejected' });
    expect(onOpen).not.toHaveBeenCalled();
  });
});
