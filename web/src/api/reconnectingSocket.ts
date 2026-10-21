/**
 * A WebSocket that reconnects itself.
 *
 * - Jittered exponential backoff between attempts (reset after a successful open).
 * - The pending reconnect timer is stored, so stop() really cancels it.
 * - Optional idle timeout: if nothing arrives for `idleTimeoutMs` the socket is
 *   treated as dead (half-open TCP, sleeping laptop, proxy drop) and replaced.
 * - Close code 1008 (policy violation, e.g. origin rejected) stops retrying.
 */

export const POLICY_VIOLATION = 1008;
/** Server is at its connection cap ("try again later"): retry, but not eagerly. */
export const TRY_AGAIN_LATER = 1013;
/** Close code we report for sockets we dropped because they went quiet. */
export const IDLE_TIMEOUT_CODE = 4000;

export interface ReconnectingSocketOptions {
  url: () => string;
  binaryType?: BinaryType;
  /** 0 disables. */
  idleTimeoutMs?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  /**
   * When set, the connection only counts as established (onOpen fires and
   * backoff resets) once a message satisfying this arrives. Servers that
   * accept and then immediately close a socket (origin rejected, connection
   * cap) never send it, so they can't reset the backoff or receive requests.
   */
  isReadyMessage?: (event: MessageEvent) => boolean;
  /** Without isReadyMessage: backoff resets once a socket has stayed open this long. */
  stableMs?: number;
  /** The connection is established (see isReadyMessage). */
  onOpen?: () => void;
  onMessage?: (event: MessageEvent) => void;
  onClose?: (code: number, reason: string) => void;
  /** Injectable for tests. */
  random?: () => number;
  createSocket?: (url: string) => WebSocket;
}

/** Equal-jitter exponential backoff: half fixed, half random. */
export function backoffDelay(
  attempt: number,
  baseMs: number,
  maxMs: number,
  random: () => number = Math.random,
): number {
  const ceiling = Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt));
  return Math.round(ceiling / 2 + random() * (ceiling / 2));
}

export function socketUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${path}`;
}

export class ReconnectingSocket {
  private ws: WebSocket | null = null;
  private running = false;
  private rejected = false;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private stableTimer: ReturnType<typeof setTimeout> | null = null;
  private ready = false;
  private readonly opts: Required<
    Pick<
      ReconnectingSocketOptions,
      'baseDelayMs' | 'maxDelayMs' | 'idleTimeoutMs' | 'random' | 'stableMs'
    >
  > &
    ReconnectingSocketOptions;

  constructor(options: ReconnectingSocketOptions) {
    this.opts = {
      baseDelayMs: 1000,
      maxDelayMs: 15_000,
      idleTimeoutMs: 0,
      stableMs: 5000,
      random: Math.random,
      ...options,
    };
  }

  get isOpen(): boolean {
    return this.ws != null && this.ws.readyState === WebSocket.OPEN;
  }

  /** Open and established (after the ready message, when one is required). */
  get isReady(): boolean {
    return this.isOpen && this.ready;
  }

  get isRejected(): boolean {
    return this.rejected;
  }

  get isRunning(): boolean {
    return this.running;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.rejected = false;
    this.attempt = 0;
    this.open();
  }

  stop(): void {
    this.running = false;
    this.ready = false;
    this.clearReconnect();
    this.clearIdle();
    this.clearStable();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      detach(ws);
      try {
        ws.close();
      } catch {
        // already closed
      }
    }
  }

  send(data: string): boolean {
    if (!this.isOpen || !this.ws) return false;
    this.ws.send(data);
    return true;
  }

  private open(): void {
    if (!this.running || this.ws) return;
    this.clearReconnect();
    const create = this.opts.createSocket ?? ((url: string) => new WebSocket(url));
    let ws: WebSocket;
    try {
      ws = create(this.opts.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    if (this.opts.binaryType) ws.binaryType = this.opts.binaryType;
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.touch();
      if (this.opts.isReadyMessage) return;
      // No handshake to wait for: report open now, but only forgive past
      // failures once the socket has proven it stays up.
      this.ready = true;
      this.stableTimer = setTimeout(() => {
        this.stableTimer = null;
        this.attempt = 0;
      }, this.opts.stableMs);
      this.opts.onOpen?.();
    };
    ws.onmessage = (event) => {
      if (this.ws !== ws) return;
      this.touch();
      if (!this.ready && this.opts.isReadyMessage?.(event)) {
        this.ready = true;
        this.attempt = 0;
        this.opts.onOpen?.();
        if (this.ws !== ws) return;
      }
      this.opts.onMessage?.(event);
    };
    ws.onerror = () => {
      // onclose always follows; closing here just makes it prompt.
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
    ws.onclose = (event) => {
      if (this.ws !== ws) return;
      this.handleClosed(event.code, event.reason);
    };
  }

  private handleClosed(code: number, reason: string): void {
    this.ws = null;
    this.ready = false;
    this.clearIdle();
    this.clearStable();
    if (code === POLICY_VIOLATION) {
      this.rejected = true;
      this.running = false;
    }
    if (code === TRY_AGAIN_LATER) this.attempt = Math.max(this.attempt, 3);
    this.opts.onClose?.(code, reason);
    if (this.running) this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    this.clearReconnect();
    const delay = backoffDelay(
      this.attempt,
      this.opts.baseDelayMs,
      this.opts.maxDelayMs,
      this.opts.random,
    );
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  /** Any traffic proves the connection is alive; push the idle deadline out. */
  private touch(): void {
    if (!this.opts.idleTimeoutMs) return;
    this.clearIdle();
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null;
      const ws = this.ws;
      if (!ws) return;
      // A dead TCP connection may never deliver onclose, so don't wait for it.
      detach(ws);
      try {
        ws.close(IDLE_TIMEOUT_CODE, 'Idle timeout');
      } catch {
        // ignore
      }
      this.handleClosed(IDLE_TIMEOUT_CODE, 'Idle timeout');
    }, this.opts.idleTimeoutMs);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearStable(): void {
    if (this.stableTimer != null) {
      clearTimeout(this.stableTimer);
      this.stableTimer = null;
    }
  }

  private clearIdle(): void {
    if (this.idleTimer != null) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }
}

function detach(ws: WebSocket): void {
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
}
