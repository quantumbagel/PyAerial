/** Convert a ``YYYY-MM-DD`` date input to local-day unix seconds. */

export function localDayStartSeconds(dateStr: string): number | null {
  if (!dateStr) return null;
  const parsed = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.getTime() / 1000;
}

export function localDayEndSeconds(dateStr: string): number | null {
  if (!dateStr) return null;
  const parsed = new Date(`${dateStr}T23:59:59.999`);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.getTime() / 1000;
}
