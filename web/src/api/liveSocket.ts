import { ApiError, apiErrorFromResponse } from './errors';
import { POLICY_VIOLATION, ReconnectingSocket, TRY_AGAIN_LATER, socketUrl } from './reconnectingSocket';
import type { LiveMessage } from './types';

export type LiveSocketHandlers = {
  onMessage?: (message: LiveMessage) => void;
  onOpen?: () => void;
  onClose?: (code?: number, reason?: string) => void;
};

const REQUEST_TIMEOUT_MS = 30_000;
const MAX_QUEUE_SIZE = 64;
/** The server pings every 15 s (pyaerial.api.broadcaster._PING_INTERVAL); three missed pings = dead. */
export const LIVE_IDLE_TIMEOUT_MS = 45_000;

type Settle = {
  resolve: (val: unknown) => void;
  reject: (err: ApiError) => void;
  timer: ReturnType<typeof setTimeout>;
};

const handlersSet = new Set<LiveSocketHandlers>();
const pendingRequests = new Map<string, Settle>();
const requestQueue: (Settle & { id: string; action: string; params: Record<string, unknown> })[] = [];
let nextId = 0;

/** The server's first message on an admitted socket (never sent before a 1008/1013 close). */
export function isHelloMessage(event: MessageEvent): boolean {
  if (typeof event.data !== 'string' || !event.data.includes('hello')) return false;
  try {
    return (JSON.parse(event.data) as { type?: string })?.type === 'hello';
  } catch {
    return false;
  }
}

const socket = new ReconnectingSocket({
  url: () => socketUrl('/ws/live'),
  idleTimeoutMs: LIVE_IDLE_TIMEOUT_MS,
  // Rejected sockets are accepted and then closed, so "open" proves nothing:
  // only the hello means we were admitted.
  isReadyMessage: isHelloMessage,
  onOpen: () => {
    handlersSet.forEach((h) => h.onOpen?.());
    flushQueue();
  },
  onMessage: (event) => handleMessage(event),
  onClose: (code, reason) => {
    rejectAllPending(code);
    handlersSet.forEach((h) => h.onClose?.(code, reason));
  },
});

function generateId(): string {
  nextId += 1;
  return `${Date.now().toString(36)}-${nextId}`;
}

function closeError(code: number): ApiError {
  if (code === POLICY_VIOLATION) return new ApiError('rejected', 'Connection rejected');
  if (code === TRY_AGAIN_LATER) return new ApiError('busy', 'Server at its connection limit');
  return new ApiError('disconnected', 'Connection closed');
}

function rejectAllPending(code: number) {
  const error = closeError(code);
  pendingRequests.forEach((req) => {
    clearTimeout(req.timer);
    req.reject(error);
  });
  pendingRequests.clear();
  // Queued requests were never sent. A rejected socket will never send them;
  // at the connection cap, fail fast so the UI can say why (it retries).
  if (code === POLICY_VIOLATION || code === TRY_AGAIN_LATER) {
    while (requestQueue.length > 0) {
      const req = requestQueue.shift()!;
      clearTimeout(req.timer);
      req.reject(error);
    }
  }
}

function scheduleTimeout(id: string): ReturnType<typeof setTimeout> {
  return setTimeout(() => {
    const pending = pendingRequests.get(id);
    if (pending) {
      pendingRequests.delete(id);
      pending.reject(new ApiError('timeout', 'Request timed out'));
    }
    const queuedIdx = requestQueue.findIndex((req) => req.id === id);
    if (queuedIdx !== -1) {
      const [queued] = requestQueue.splice(queuedIdx, 1);
      queued.reject(new ApiError('timeout', 'Request timed out'));
    }
  }, REQUEST_TIMEOUT_MS);
}

function sendRequest(id: string, action: string, params: Record<string, unknown>): boolean {
  return socket.send(JSON.stringify({ type: 'request', id, action, params }));
}

function flushQueue() {
  while (requestQueue.length > 0 && socket.isReady) {
    const req = requestQueue.shift()!;
    pendingRequests.set(req.id, req);
    if (!sendRequest(req.id, req.action, req.params)) {
      pendingRequests.delete(req.id);
      requestQueue.unshift(req);
      return;
    }
  }
}

function handleMessage(event: MessageEvent) {
  let data: unknown;
  try {
    data = JSON.parse(event.data as string);
  } catch (e) {
    console.error('[WS] Invalid message:', e);
    return;
  }
  if (!data || typeof data !== 'object') return;
  const msg = data as {
    type?: string;
    id?: string;
    success?: boolean;
    data?: unknown;
    error?: string;
    code?: string;
  };
  if (msg.type === 'response') {
    const req = msg.id ? pendingRequests.get(msg.id) : undefined;
    if (!req || !msg.id) return;
    clearTimeout(req.timer);
    pendingRequests.delete(msg.id);
    if (msg.success) req.resolve(msg.data);
    else req.reject(apiErrorFromResponse(msg.code, msg.error));
    return;
  }
  // Pings only keep the idle timer alive (handled by ReconnectingSocket).
  if (msg.type === 'ping') return;
  handlersSet.forEach((h) => {
    try {
      h.onMessage?.(data as LiveMessage);
    } catch (e) {
      console.error('[WS] Handler failed:', e);
    }
  });
}

export function connectLiveSocket(handlers: LiveSocketHandlers): () => void {
  handlersSet.add(handlers);
  if (socket.isReady) {
    handlers.onOpen?.();
  } else if (!socket.isRunning && !socket.isRejected) {
    socket.start();
  }
  return () => {
    handlersSet.delete(handlers);
  };
}

export function isLiveSocketOpen(): boolean {
  return socket.isReady;
}

export function sendWsRequest<T>(action: string, params: Record<string, unknown> = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = generateId();
    const settle: Settle = {
      resolve: resolve as (val: unknown) => void,
      reject,
      timer: scheduleTimeout(id),
    };

    if (socket.isRejected) {
      clearTimeout(settle.timer);
      reject(new ApiError('rejected', 'Connection rejected'));
      return;
    }

    if (socket.isReady) {
      pendingRequests.set(id, settle);
      if (sendRequest(id, action, params)) return;
      pendingRequests.delete(id);
    }

    if (requestQueue.length >= MAX_QUEUE_SIZE) {
      clearTimeout(settle.timer);
      reject(new ApiError('queue_full', 'Request queue full'));
      return;
    }

    requestQueue.push({ id, action, params, ...settle });
    if (!socket.isRunning) socket.start();
  });
}
