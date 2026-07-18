import type { LiveMessage } from './types';

export type LiveSocketHandlers = {
  onMessage: (message: LiveMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let isClosed = false;
let backoff = 1000;
const handlersSet = new Set<LiveSocketHandlers>();
const pendingRequests = new Map<string, { resolve: (val: any) => void; reject: (err: any) => void }>();
const requestQueue: { id: string; action: string; params: any; resolve: any; reject: any }[] = [];

function generateId(): string {
  return Math.random().toString(36).substring(2, 15);
}

function flushQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  console.log(`[WS] Flushing ${requestQueue.length} queued request(s)`);
  while (requestQueue.length > 0) {
    const req = requestQueue.shift();
    if (req) {
      pendingRequests.set(req.id, { resolve: req.resolve, reject: req.reject });
      console.log(`[WS] Sending queued request: ${req.action} (id: ${req.id})`, req.params);
      ws.send(JSON.stringify({
        type: 'request',
        id: req.id,
        action: req.action,
        params: req.params
      }));
    }
  }
}

function connect() {
  isClosed = false;
  if (ws) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  console.log(`[WS] Connecting to ${protocol}//${window.location.host}/ws/live`);
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/live`);

  ws.onopen = () => {
    console.log('[WS] Connection established');
    backoff = 1000;
    handlersSet.forEach((h) => h.onOpen?.());
    flushQueue();
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data && data.type === 'response') {
        console.log(`[WS] Received response for ${data.id}: success=${data.success}`, data.data || data.error);
        const req = pendingRequests.get(data.id);
        if (req) {
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
    console.warn('[WS] Connection closed');
    ws = null;
    handlersSet.forEach((h) => h.onClose?.());
    pendingRequests.forEach((req) => req.reject(new Error('Connection closed')));
    pendingRequests.clear();
    
    if (!isClosed) {
      reconnectTimer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 10000);
    }
  };

  ws.onerror = (e) => {
    console.error('[WS] Connection error:', e);
    ws?.close();
  };
}

export function subscribeLiveSocket(handlers: LiveSocketHandlers): () => void {
  handlersSet.add(handlers);
  if (!ws) {
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    handlers.onOpen?.();
  }
  return () => {
    handlersSet.delete(handlers);
    if (handlersSet.size === 0) {
      console.log('[WS] No more subscribers, closing connection');
      isClosed = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      ws?.close();
      ws = null;
    }
  };
}

export function connectLiveSocket(handlers: LiveSocketHandlers): () => void {
  return subscribeLiveSocket(handlers);
}

export function sendWsRequest<T>(action: string, params: any = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = generateId();
    if (ws && ws.readyState === WebSocket.OPEN) {
      pendingRequests.set(id, { resolve, reject });
      console.log(`[WS] Sending request: ${action} (id: ${id})`, params);
      ws.send(JSON.stringify({
        type: 'request',
        id,
        action,
        params
      }));
    } else {
      console.log(`[WS] Queueing request: ${action} (id: ${id})`, params);
      requestQueue.push({ id, action, params, resolve, reject });
      if (!ws) {
        connect();
      }
    }
  });
}
