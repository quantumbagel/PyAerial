const BEAST_ESC = 0x1a;
const BEAST_MSG_LEN: Record<number, number> = { 0x31: 2, 0x32: 7, 0x33: 14 };

export interface DecodedBeastFrame {
  hex: string;
  rssi?: number;
  clock: number;
  df?: number;
  icao?: string;
}

export function beastRssiDbfs(level: number): number | undefined {
  if (level <= 0) return undefined;
  return 20 * Math.log10(level / 255);
}

export function frameFields(hexMsg: string): { hex: string; df?: number; icao?: string } {
  const hex = hexMsg.toLowerCase().trim();
  const fields: { hex: string; df?: number; icao?: string } = { hex };
  if (hex.length < 2 || hex.length % 2) return fields;
  const header = Number.parseInt(hex.slice(0, 2), 16);
  if (!Number.isFinite(header)) return fields;
  const df = (header >> 3) & 0x1f;
  fields.df = df;
  if ((df === 17 || df === 18) && hex.length >= 8) {
    fields.icao = hex.slice(2, 8);
  }
  return fields;
}

function readUnescaped(
  data: Uint8Array,
  start: number,
  count: number,
): { payload: Uint8Array | null; next: number | null } {
  const out: number[] = [];
  let index = start;
  while (out.length < count) {
    if (index >= data.length) return { payload: null, next: null };
    const byte = data[index];
    index += 1;
    if (byte === BEAST_ESC) {
      if (index >= data.length) return { payload: null, next: null };
      if (data[index] !== BEAST_ESC) return { payload: null, next: index - 1 };
      index += 1;
      out.push(BEAST_ESC);
    } else {
      out.push(byte);
    }
  }
  return { payload: Uint8Array.from(out), next: index };
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function tryParseBeast(
  buffer: Uint8Array,
): { frame: DecodedBeastFrame | null; consumed: number } {
  const start = buffer.indexOf(BEAST_ESC);
  if (start < 0) return { frame: null, consumed: buffer.length };
  if (start + 2 > buffer.length) return { frame: null, consumed: start };
  const kind = buffer[start + 1];
  if (kind === BEAST_ESC) return { frame: null, consumed: start + 1 };
  const msgLen = BEAST_MSG_LEN[kind];
  if (msgLen == null) return { frame: null, consumed: start + 1 };
  const { payload, next } = readUnescaped(buffer, start + 2, 6 + 1 + msgLen);
  if (payload == null) {
    if (next != null) {
      if (next <= start) return { frame: null, consumed: start + 1 };
      return { frame: null, consumed: next };
    }
    if (start) return { frame: null, consumed: start };
    return { frame: null, consumed: 0 };
  }
  const clock =
    payload[0] * 2 ** 40 +
    payload[1] * 2 ** 32 +
    payload[2] * 2 ** 24 +
    payload[3] * 2 ** 16 +
    payload[4] * 2 ** 8 +
    payload[5];
  const rssi = beastRssiDbfs(payload[6]);
  const hex = bytesToHex(payload.subarray(7));
  return { frame: { ...frameFields(hex), rssi, clock }, consumed: next ?? buffer.length };
}

export class BeastParser {
  private buf = new Uint8Array(0);

  reset(): void {
    this.buf = new Uint8Array(0);
  }

  feed(chunk: Uint8Array): DecodedBeastFrame[] {
    const next = new Uint8Array(this.buf.length + chunk.length);
    next.set(this.buf);
    next.set(chunk, this.buf.length);
    this.buf = next;
    const frames: DecodedBeastFrame[] = [];
    while (this.buf.length) {
      const { frame, consumed } = tryParseBeast(this.buf);
      if (consumed === 0) break;
      if (frame) frames.push(frame);
      this.buf = this.buf.subarray(consumed);
    }
    if (this.buf.length > 65_536) this.buf = new Uint8Array(0);
    return frames;
  }
}
