import type { LiveMessage } from './types';

export type LiveSocketHandlers = {
  onMessage: (message: LiveMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

const REQUEST_TIMEOUT_MS = 30_000;
const MAX_QUEUE_SIZE = 64;

let ws: WebSocket | null = null;
let isClosed = false;
let backoff = 1000;
const handlersSet = new Set<LiveSocketHandlers>();
const pendingRequests = new Map<
  string,
  { resolve: (val: unknown) => void; reject: (err: Error) => void; timer: ReturnType<typeof setTimeout> }
>();
const requestQueue: {
  id: string;
  action: string;
  params: Record<string, unknown>;
  resolve: (val: unknown) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}[] = [];

function generateId(): string {
  return Math.random().toString(36).substring(2, 15);
}

function rejectAllPending(reason: string) {
  pendingRequests.forEach((req) => {
    clearTimeout(req.timer);
    req.reject(new Error(reason));
  });
  pendingRequests.clear();
  while (requestQueue.length > 0) {
    const req = requestQueue.shift();
    if (req) {
      clearTimeout(req.timer);
      req.reject(new Error(reason));
    }
  }
}

function scheduleTimeout(id: string): ReturnType<typeof setTimeout> {
  return setTimeout(() => {
    const pending = pendingRequests.get(id);
    if (pending) {
      pendingRequests.delete(id);
      pending.reject(new Error('Request timed out'));
    }
    const queuedIdx = requestQueue.findIndex((req) => req.id === id);
    if (queuedIdx !== -1) {
      const [queued] = requestQueue.splice(queuedIdx, 1);
      clearTimeout(queued.timer);
      queued.reject(new Error('Request timed out'));
    }
  }, REQUEST_TIMEOUT_MS);
}

function flushQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  while (requestQueue.length > 0) {
    const req = requestQueue.shift();
    if (!req) break;
    pendingRequests.set(req.id, {
      resolve: req.resolve,
      reject: req.reject,
      timer: req.timer,
    });
    ws.send(
      JSON.stringify({
        type: 'request',
        id: req.id,
        action: req.action,
        params: req.params,
      }),
    );
  }
}

function connect() {
  isClosed = false;
  if (ws) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/live`);

  ws.onopen = () => {
    backoff = 1000;
    handlersSet.forEach((h) => h.onOpen?.());
    flushQueue();
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data && data.type === 'response') {
        const req = pendingRequests.get(data.id);
        if (req) {
          clearTimeout(req.timer);
          pendingRequests.delete(data.id);
          if (data.success) {
            req.resolve(data.data);
          } else {
            req.reject(new Error(data.error || 'Request failed'));
          }
        }
      } else {
        handlersSet.forEach((h) => h.onMessage(data as LiveMessage));
      }
    } catch (e) {
      console.error('[WS] Error processing message:', e);
    }
  };

  ws.onclose = () => {
    ws = null;
    handlersSet.forEach((h) => h.onClose?.());
    rejectAllPending('Connection closed');

    if (!isClosed) {
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 10000);
    }
  };

  ws.onerror = () => {
    ws?.close();
  };
}

export function resetLiveSocketForTests(): void {
  isClosed = true;
  ws?.close();
  ws = null;
  handlersSet.clear();
  rejectAllPending('test reset');
  requestQueue.length = 0;
  backoff = 1000;
  isClosed = false;
}

export function connectLiveSocket(handlers: LiveSocketHandlers): () => void {
  handlersSet.add(handlers);
  if (!ws) {
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    handlers.onOpen?.();
  }
  return () => {
    handlersSet.delete(handlers);
  };
}

export function sendWsRequest<T>(action: string, params: Record<string, unknown> = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = generateId();
    const timer = scheduleTimeout(id);
    const settle = {
      resolve: resolve as (val: unknown) => void,
      reject,
      timer,
    };

    if (ws && ws.readyState === WebSocket.OPEN) {
      pendingRequests.set(id, settle);
      ws.send(
        JSON.stringify({
          type: 'request',
          id,
          action,
          params,
        }),
      );
      return;
    }

    if (requestQueue.length >= MAX_QUEUE_SIZE) {
      clearTimeout(timer);
      reject(new Error('Request queue full'));
      return;
    }

    requestQueue.push({ id, action, params, ...settle });
    if (!ws) {
      connect();
    }
  });
}
