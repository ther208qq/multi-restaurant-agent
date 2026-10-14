/**
 * Wall-clock time formatting.
 *
 * The backend stores reservation / order times as UTC but means them to be read
 * as the wall clock the customer typed ("booking at 19:00"). Using
 * `new Date(iso).toLocaleString()` would shift that 19:00 into the browser's
 * timezone (UTC+8 shows it as the next day 03:00) and disagree with the chat
 * reply, which renders the stored value with `strftime`. These helpers instead
 * read the ISO string's own components, so every surface agrees.
 */

function parseParts(iso) {
  if (!iso) return null;
  const match = String(iso).match(
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/
  );
  if (!match) return null;
  const [, year, month, day, hour, minute] = match;
  return { year, month, day, hour, minute };
}

export function formatWallClockDate(iso) {
  const p = parseParts(iso);
  return p ? `${p.year}-${p.month}-${p.day}` : '';
}

export function formatWallClockTime(iso) {
  const p = parseParts(iso);
  return p ? `${p.hour}:${p.minute}` : '';
}

export function formatWallClockDateTime(iso) {
  const p = parseParts(iso);
  return p ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}` : '';
}
