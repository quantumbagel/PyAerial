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
/** Close code we report for sockets we dropped because they went quiet. */
export const IDLE_TIMEOUT_CODE = 4000;

export interface ReconnectingSocketOptions {
  url: () => string;
  binaryType?: BinaryType;
  /** 0 disables. */
  idleTimeoutMs?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
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
  private readonly opts: Required<
    Pick<ReconnectingSocketOptions, 'baseDelayMs' | 'maxDelayMs' | 'idleTimeoutMs' | 'random'>
  > &
    ReconnectingSocketOptions;

  constructor(options: ReconnectingSocketOptions) {
    this.opts = {
      baseDelayMs: 1000,
      maxDelayMs: 15_000,
      idleTimeoutMs: 0,
      random: Math.random,
      ...options,
    };
  }

  get isOpen(): boolean {
    return this.ws != null && this.ws.readyState === WebSocket.OPEN;
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
    this.clearReconnect();
    this.clearIdle();
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
      this.attempt = 0;
      this.touch();
      this.opts.onOpen?.();
    };
    ws.onmessage = (event) => {
      if (this.ws !== ws) return;
      this.touch();
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
    this.clearIdle();
    if (code === POLICY_VIOLATION) {
      this.rejected = true;
      this.running = false;
    }
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
