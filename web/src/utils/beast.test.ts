import { describe, expect, it } from 'vitest';
import { BeastParser, beastRssiDbfs, frameFields } from './beast';

const BEAST_ESC = 0x1a;

function encodeBeast(hexMsg: string, clock = 1, signal = 128): Uint8Array {
  const payload = Uint8Array.from(hexMsg.match(/.{2}/g)!.map((byte) => Number.parseInt(byte, 16)));
  const clockBytes = [
    (clock / 2 ** 40) & 0xff,
    (clock / 2 ** 32) & 0xff,
    (clock / 2 ** 24) & 0xff,
    (clock / 2 ** 16) & 0xff,
    (clock / 2 ** 8) & 0xff,
    clock & 0xff,
  ];
  const body = [...clockBytes, signal, ...payload];
  const out = [BEAST_ESC, 0x33];
  for (const byte of body) {
    out.push(byte);
    if (byte === BEAST_ESC) out.push(BEAST_ESC);
  }
  return Uint8Array.from(out);
}

describe('frameFields', () => {
  it('extracts DF17 ICAO', () => {
    expect(frameFields('8d406b902015a678d4d220aa4bda')).toEqual({
      hex: '8d406b902015a678d4d220aa4bda',
      df: 17,
      icao: '406b90',
    });
  });
});

describe('BeastParser', () => {
  it('decodes a long Mode S frame', () => {
    const hex = '8d406b902015a678d4d220aa4bda';
    const frames = new BeastParser().feed(encodeBeast(hex, 12, 128));
    expect(frames).toHaveLength(1);
    expect(frames[0].hex).toBe(hex);
    expect(frames[0].clock).toBe(12);
    expect(frames[0].icao).toBe('406b90');
    expect(frames[0].rssi).toBeCloseTo(beastRssiDbfs(128)!, 6);
  });

  it('handles 0x1a escapes', () => {
    const hex = '8d1a6b902015a678d4d220aa4bda';
    const frames = new BeastParser().feed(encodeBeast(hex, 1, 0x1a));
    expect(frames[0].hex).toBe(hex);
    expect(frames[0].rssi).toBe(beastRssiDbfs(0x1a));
  });

  it('drops a partial buffer on reset', () => {
    const hex = '8d406b902015a678d4d220aa4bda';
    const raw = encodeBeast(hex, 12, 128);
    const parser = new BeastParser();
    expect(parser.feed(raw.slice(0, 5))).toEqual([]);
    parser.reset();
    const frames = parser.feed(raw);
    expect(frames).toHaveLength(1);
    expect(frames[0].hex).toBe(hex);
  });
});
