import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { parseRawMessage } from './rawSocket';

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
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  message(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data }));
  }
}

beforeEach(() => {
  vi.resetModules();
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('parseRawMessage', () => {
  it('reads server timestamp, receiver, receivers, rssi, clock, df, and icao', () => {
    const frames = parseRawMessage(
      JSON.stringify({
        type: 'raw',
        frames: [
          {
            hex: '8d406b902015a678d4d220aa4bda',
            timestamp: 1.5,
            receiver: 'strong',
            receivers: ['strong', 'weak'],
            rssi: -8.0,
            clock: 12,
            df: 17,
            icao: '406b90',
          },
        ],
      }),
    );
    expect(frames).toEqual([
      {
        hex: '8d406b902015a678d4d220aa4bda',
        timestamp: 1.5,
        receiver: 'strong',
        receivers: ['strong', 'weak'],
        rssi: -8.0,
        clock: 12,
        df: 17,
        icao: '406b90',
      },
    ]);
  });

  it('ignores binary, malformed, and non-raw messages', () => {
    expect(parseRawMessage(new ArrayBuffer(4))).toEqual([]);
    expect(parseRawMessage('{not json')).toEqual([]);
    expect(parseRawMessage(JSON.stringify({ type: 'ping' }))).toEqual([]);
    expect(parseRawMessage(JSON.stringify({ type: 'raw', frames: [{ timestamp: 1 }] }))).toEqual([]);
  });
});

describe('connectRawSocket', () => {
  it('opens /ws/raw only while a subscriber is attached and forwards JSON frames', async () => {
    const raw = await import('./rawSocket');
    const onFrames = vi.fn();
    const disconnect = raw.connectRawSocket({ onFrames });
    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0].url).toMatch(/\/ws\/raw$/);
    FakeSocket.instances[0].open();
    FakeSocket.instances[0].message(
      JSON.stringify({
        type: 'raw',
        frames: [{ hex: '8d406b90', timestamp: 9.25, receiver: 'main' }],
      }),
    );
    expect(onFrames).toHaveBeenCalledWith([{ hex: '8d406b90', timestamp: 9.25, receiver: 'main' }]);
    disconnect();
    expect(FakeSocket.instances[0].readyState).toBe(FakeSocket.CLOSED);
  });
});
