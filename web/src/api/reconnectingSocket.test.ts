import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { IDLE_TIMEOUT_CODE, ReconnectingSocket, backoffDelay } from './reconnectingSocket';

class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState: number = WebSocket.CONNECTING;
  binaryType: BinaryType = 'blob';
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: string[] = [];
  closedWith: number | undefined;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close(code?: number) {
    this.closedWith = code;
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

function makeSocket(overrides: Partial<ConstructorParameters<typeof ReconnectingSocket>[0]> = {}) {
  const onOpen = vi.fn();
  const onClose = vi.fn();
  const onMessage = vi.fn();
  const sock = new ReconnectingSocket({
    url: () => 'ws://test/ws',
    baseDelayMs: 1000,
    maxDelayMs: 8000,
    random: () => 1,
    createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
    onOpen,
    onClose,
    onMessage,
    ...overrides,
  });
  return { sock, onOpen, onClose, onMessage };
}

describe('backoffDelay', () => {
  it('grows exponentially up to the cap with jitter in [ceiling/2, ceiling]', () => {
    expect(backoffDelay(0, 1000, 10_000, () => 0)).toBe(500);
    expect(backoffDelay(0, 1000, 10_000, () => 1)).toBe(1000);
    expect(backoffDelay(3, 1000, 10_000, () => 1)).toBe(8000);
    expect(backoffDelay(10, 1000, 10_000, () => 1)).toBe(10_000);
    expect(backoffDelay(10, 1000, 10_000, () => 0)).toBe(5000);
  });
});

describe('ReconnectingSocket', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reconnects with growing delays and resets once a connection holds', () => {
    const { sock, onClose } = makeSocket();
    sock.start();
    expect(FakeSocket.instances).toHaveLength(1);

    FakeSocket.instances[0].serverClose();
    expect(onClose).toHaveBeenCalledWith(1006, '');
    vi.advanceTimersByTime(999);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);

    FakeSocket.instances[1].serverClose();
    vi.advanceTimersByTime(1999);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(3);

    FakeSocket.instances[2].serverOpen();
    vi.advanceTimersByTime(5000); // stayed up: backoff forgiven
    FakeSocket.instances[2].serverClose();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances).toHaveLength(4);
  });

  it('stop() cancels a pending reconnect', () => {
    const { sock } = makeSocket();
    sock.start();
    FakeSocket.instances[0].serverClose();
    sock.stop();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it('does not retry after a policy rejection', () => {
    const { sock, onClose } = makeSocket();
    sock.start();
    FakeSocket.instances[0].serverClose(1008, 'Origin not allowed');
    expect(onClose).toHaveBeenCalledWith(1008, 'Origin not allowed');
    expect(sock.isRejected).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it('forces a reconnect when the connection goes quiet', () => {
    const { sock, onClose, onMessage } = makeSocket({ idleTimeoutMs: 45_000 });
    sock.start();
    const first = FakeSocket.instances[0];
    first.serverOpen();

    vi.advanceTimersByTime(30_000);
    first.serverMessage('{"type":"ping"}');
    expect(onMessage).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(30_000);
    expect(onClose).not.toHaveBeenCalled();

    vi.advanceTimersByTime(15_000);
    expect(onClose).toHaveBeenCalledWith(IDLE_TIMEOUT_CODE, 'Idle timeout');
    expect(first.closedWith).toBe(IDLE_TIMEOUT_CODE);
    expect(first.onmessage).toBeNull();

    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it('ignores events from a socket it already replaced', () => {
    const { sock, onMessage } = makeSocket({ idleTimeoutMs: 1000 });
    sock.start();
    const first = FakeSocket.instances[0];
    first.serverOpen();
    vi.advanceTimersByTime(1000);
    expect(first.onmessage).toBeNull();
    vi.advanceTimersByTime(1000);
    const second = FakeSocket.instances[1];
    second.serverOpen();
    second.serverMessage('x');
    expect(onMessage).toHaveBeenCalledTimes(1);
  });
});

describe('ReconnectingSocket handshake', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const isHello = (e: MessageEvent) => e.data === 'hello';

  it('only reports open after the ready message', () => {
    const { sock, onOpen } = makeSocket({ isReadyMessage: isHello });
    sock.start();
    const ws = FakeSocket.instances[0];
    ws.serverOpen();
    expect(onOpen).not.toHaveBeenCalled();
    expect(sock.isReady).toBe(false);
    ws.serverMessage('hello');
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(sock.isReady).toBe(true);
  });

  it('keeps backing off when the server accepts then closes (connection cap)', () => {
    const { sock, onOpen } = makeSocket({ isReadyMessage: isHello, maxDelayMs: 60_000 });
    sock.start();
    // 1013 bumps the attempt to 3 (8 s); an accepted-then-closed socket
    // must not reset it back to 1 s.
    FakeSocket.instances[0].serverOpen();
    FakeSocket.instances[0].serverClose(1013, 'too many connections');
    vi.advanceTimersByTime(8000);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.instances[1].serverOpen();
    FakeSocket.instances[1].serverClose(1013, 'too many connections');
    vi.advanceTimersByTime(8000);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(8000);
    expect(FakeSocket.instances).toHaveLength(3);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('without a handshake, forgives failures only after the socket stays up', () => {
    const { sock, onOpen } = makeSocket({ stableMs: 5000 });
    sock.start();
    FakeSocket.instances[0].serverClose();
    vi.advanceTimersByTime(1000);
    FakeSocket.instances[1].serverOpen();
    expect(onOpen).toHaveBeenCalledTimes(1);
    FakeSocket.instances[1].serverClose();
    // Closed before stableMs: the next delay is still the grown one (2 s).
    vi.advanceTimersByTime(1999);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(3);
  });
});
