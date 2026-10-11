// Heures absolues dans le fuseau configuré (--tz) ou celui du navigateur.

export function resolveTz(tz: string | null | undefined): string {
  if (tz) {
    try {
      new Intl.DateTimeFormat("fr-FR", { timeZone: tz });
      return tz;
    } catch {
      /* fuseau inconnu du navigateur */
    }
  }
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

export interface ZonedParts {
  y: number;
  m: number;
  d: number;
  hh: number;
  mm: number;
  ss: number;
}

const partsCache = new Map<string, Intl.DateTimeFormat>();

export function zonedParts(epoch: number, tz: string): ZonedParts {
  let f = partsCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
    partsCache.set(tz, f);
  }
  const p: Record<string, number> = {};
  for (const part of f.formatToParts(new Date(epoch * 1000))) {
    if (part.type !== "literal") p[part.type] = Number(part.value);
  }
  return { y: p.year, m: p.month, d: p.day, hh: p.hour, mm: p.minute, ss: p.second };
}

/** Date + heure murale dans `tz` -> epoch (s). */
export function zonedToEpoch(y: number, m: number, d: number, secOfDay: number, tz: string): number {
  const naive = Date.UTC(y, m - 1, d) / 1000 + secOfDay;
  let guess = naive;
  for (let i = 0; i < 3; i++) {
    const p = zonedParts(guess, tz);
    const shown = Date.UTC(p.y, p.m - 1, p.d, p.hh, p.mm, p.ss) / 1000;
    const diff = shown - naive;
    if (diff === 0) break;
    guess -= diff;
  }
  return guess;
}

/** Numéro de jour (jours depuis l'epoch) de la date murale, pour comparer. */
export function dayNumber(epoch: number, tz: string): number {
  const p = zonedParts(epoch, tz);
  return Date.UTC(p.y, p.m - 1, p.d) / 86400000;
}

const pad = (n: number) => String(n).padStart(2, "0");

export function clock(epoch: number, tz: string, withSeconds = true): string {
  const p = zonedParts(epoch, tz);
  return withSeconds ? `${pad(p.hh)}:${pad(p.mm)}:${pad(p.ss)}` : `${pad(p.hh)}:${pad(p.mm)}`;
}

const WEEKDAYS = ["dim.", "lun.", "mar.", "mer.", "jeu.", "ven.", "sam."];

/** "aujourd'hui 11:04:12", "hier 11:04:12", "lun. 06/10 11:04:12". */
export function relativeDay(epoch: number, tz: string, now = Date.now() / 1000): string {
  const diff = dayNumber(now, tz) - dayNumber(epoch, tz);
  if (diff === 0) return "aujourd'hui";
  if (diff === 1) return "hier";
  if (diff === 2) return "avant-hier";
  const p = zonedParts(epoch, tz);
  const wd = WEEKDAYS[new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay()];
  const sameYear = zonedParts(now, tz).y === p.y;
  return `${wd} ${pad(p.d)}/${pad(p.m)}${sameYear ? "" : "/" + p.y}`;
}

export function absolute(epoch: number, tz: string, now?: number): string {
  return `${relativeDay(epoch, tz, now)} ${clock(epoch, tz)}`;
}

export function duration(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
