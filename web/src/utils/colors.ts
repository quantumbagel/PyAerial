/**
 * Single source for the colours shared by CSS and the map (Leaflet/SVG).
 *
 * `applyPaletteTokens` writes these onto :root as custom properties at
 * startup, so styles/tokens.css and the map can never drift apart. CSS-only
 * surface colours (backgrounds, borders, text) stay in tokens.css.
 */
export const PALETTE = {
  accent: '#3b82f6',
  accentSoft: '#dbeafe',
  live: '#34d399',
  liveDark: '#047857',
  warn: '#f59e0b',
  warnDark: '#78350f',
  alert: '#ef4444',
  alertDark: '#7f1d1d',
  neutral: '#64748b',
  neutralDark: '#334155',
  home: '#38bdf8',
  white: '#ffffff',
} as const;

/** Fallback zone colours when a zone has no configured colour. */
export const ZONE_PALETTE = ['#f59e0b', '#3b82f6', '#a855f7', '#14b8a6'] as const;

const CSS_TOKENS: Record<string, string> = {
  '--accent': PALETTE.accent,
  '--accent-soft': PALETTE.accentSoft,
  '--status-live': PALETTE.live,
  '--warn': PALETTE.warn,
  '--alert': PALETTE.alert,
  '--neutral': PALETTE.neutral,
  '--neutral-dark': PALETTE.neutralDark,
  '--home': PALETTE.home,
};

export function applyPaletteTokens(root: HTMLElement = document.documentElement): void {
  for (const [name, value] of Object.entries(CSS_TOKENS)) {
    root.style.setProperty(name, value);
  }
}

export function parseHex(hex: string): [number, number, number] | null {
  const clean = hex.replace('#', '').trim();
  if (/^[0-9a-f]{3}$/i.test(clean)) {
    return [0, 1, 2].map((i) => parseInt(clean[i] + clean[i], 16)) as [number, number, number];
  }
  if (/^[0-9a-f]{6}$/i.test(clean)) {
    return [0, 2, 4].map((i) => parseInt(clean.slice(i, i + 2), 16)) as [number, number, number];
  }
  return null;
}

function toHex(rgb: [number, number, number]): string {
  return `#${rgb.map((c) => Math.max(0, Math.min(255, Math.round(c))).toString(16).padStart(2, '0')).join('')}`;
}

/** Mix a colour toward black (amount 0..1). Unparseable colours pass through. */
export function shade(hex: string, amount: number): string {
  const rgb = parseHex(hex);
  if (!rgb) return hex;
  return toHex(rgb.map((c) => c * (1 - amount)) as [number, number, number]);
}
