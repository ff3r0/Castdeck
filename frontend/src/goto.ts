// Champ "aller à" :
//   11:04 | 11:04:12          heure absolue (jour : session courante, sinon la plus récente)
//   hier 11:00 | avant-hier 9h | 2026-10-10 11:00 | 10/10 11:00
//   +5m | -30s | +1h30m | +90  relatif à la position courante
//   @12:30 | @00:12:30         position dans la session
//   00:12:30                   heure absolue si une session la couvre, sinon position
import { dayNumber, zonedParts, zonedToEpoch } from "./timefmt";

export type Goto =
  | { kind: "relative"; seconds: number }
  | { kind: "offset"; seconds: number }
  | { kind: "clock"; day: DaySpec | null; secOfDay: number; raw: string };

export type DaySpec = { rel: number } | { y: number | null; m: number; d: number };

const DAY_WORDS: Record<string, number> = {
  "aujourd'hui": 0,
  aujourdhui: 0,
  auj: 0,
  today: 0,
  hier: 1,
  yesterday: 1,
  "avant-hier": 2,
  avanthier: 2,
};

function parseClock(s: string): number | null {
  const m = /^(\d{1,2})(?:[:h](\d{2})?)?(?::(\d{2}))?$/i.exec(s);
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2] ?? 0);
  const se = Number(m[3] ?? 0);
  // "9" seul n'est pas une heure : il faut ":" ou "h"
  if (m[2] === undefined && !/h$/i.test(s)) return null;
  if (h > 23 || mi > 59 || se > 59) return null;
  return h * 3600 + mi * 60 + se;
}

function parseDuration(s: string): number | null {
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
  const re = /(\d+(?:\.\d+)?)\s*(h|m|min|s)/gi;
  let total = 0;
  let consumed = "";
  let m: RegExpExecArray | null;
  while ((m = re.exec(s))) {
    const v = Number(m[1]);
    const u = m[2].toLowerCase();
    total += u === "h" ? v * 3600 : u === "s" ? v : v * 60;
    consumed += m[0];
  }
  return consumed.replace(/\s/g, "") === s.replace(/\s/g, "") && consumed ? total : null;
}

function parseOffsetClock(s: string): number | null {
  const parts = s.split(":").map(Number);
  if (parts.some((p) => !Number.isFinite(p) || p < 0) || parts.length < 2 || parts.length > 3) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

export function parseGoto(input: string): Goto | null {
  const s = input.trim().toLowerCase().replace(/\s+/g, " ");
  if (!s) return null;
  if (/^[+-]/.test(s)) {
    const v = parseDuration(s.slice(1).trim());
    return v === null ? null : { kind: "relative", seconds: s[0] === "-" ? -v : v };
  }
  if (s.startsWith("@")) {
    const body = s.slice(1).trim();
    const v = body.includes(":") ? parseOffsetClock(body) : parseDuration(body);
    return v === null ? null : { kind: "offset", seconds: v };
  }
  const tokens = s.split(" ");
  const clockTok = tokens.pop()!;
  const secOfDay = parseClock(clockTok);
  if (secOfDay === null) return null;
  const dayStr = tokens.join(" ").replace(/^(le|à|a)\s+/, "").replace(/\s+(à|a)$/, "");
  let day: DaySpec | null = null;
  if (dayStr) {
    if (dayStr in DAY_WORDS) day = { rel: DAY_WORDS[dayStr] };
    else {
      let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dayStr);
      if (m) day = { y: +m[1], m: +m[2], d: +m[3] };
      else if ((m = /^(\d{1,2})\/(\d{1,2})(?:\/(\d{4}))?$/.exec(dayStr))) {
        day = { y: m[3] ? +m[3] : null, m: +m[2], d: +m[1] };
      } else return null;
    }
  }
  return { kind: "clock", day, secOfDay, raw: clockTok };
}

export interface SessionSpan {
  id: string;
  start: number;
  duration: number;
}

export type Target = { sessionId: string; offset: number; note?: string };

/** Résout un "aller à" en (session, offset en temps d'origine). */
export function resolveGoto(
  g: Goto,
  sessions: SessionSpan[],
  current: { id: string; offset: number } | null,
  tz: string,
  now = Date.now() / 1000,
): Target | null {
  const cur = current ? sessions.find((s) => s.id === current.id) : undefined;
  if (g.kind === "relative") {
    if (!cur || !current) return null;
    return clampTo(cur, current.offset + g.seconds);
  }
  if (g.kind === "offset") {
    if (!cur) return null;
    return clampTo(cur, g.seconds);
  }
  const containing = (t: number) =>
    sessions.filter((s) => s.start <= t && t <= s.start + s.duration);
  if (g.day) {
    let y: number, m: number, d: number;
    if ("rel" in g.day) {
      const q = dateOfDay(dayNumber(now, tz) - g.day.rel);
      [y, m, d] = [q.y, q.m, q.d];
    } else {
      y = g.day.y ?? zonedParts(now, tz).y;
      m = g.day.m;
      d = g.day.d;
    }
    const t = zonedToEpoch(y, m, d, g.secOfDay, tz);
    return pickAt(t, containing(t), sessions, cur);
  }
  // Pas de jour : chaque jour couvert par une session est candidat.
  const candidates: number[] = [];
  for (const s of sessions) {
    const first = dayNumber(s.start, tz);
    const last = dayNumber(s.start + s.duration, tz);
    for (let dn = first; dn <= last; dn++) {
      const q = dateOfDay(dn);
      const t = zonedToEpoch(q.y, q.m, q.d, g.secOfDay, tz);
      if (s.start <= t && t <= s.start + s.duration) candidates.push(t);
    }
  }
  if (candidates.length) {
    // priorité à la session courante, puis à la plus récente
    if (cur) {
      const inCur = candidates.filter((t) => cur.start <= t && t <= cur.start + cur.duration);
      if (inCur.length) return pickAt(Math.max(...inCur), [cur], sessions, cur);
    }
    const t = Math.max(...candidates);
    return pickAt(t, containing(t), sessions, cur);
  }
  // "00:12:30" sans session à cette heure-là : position dans la session courante
  if (cur && g.raw.includes(":")) {
    const v = parseOffsetClock(g.raw);
    if (v !== null && v <= cur.duration) return clampTo(cur, v);
  }
  return null;
}

/** Date (y, m, d) d'un numéro de jour (jours depuis l'epoch). */
function dateOfDay(dn: number) {
  return zonedParts(dn * 86400 + 43200, "UTC");
}

function clampTo(s: SessionSpan, offset: number): Target {
  return { sessionId: s.id, offset: Math.min(Math.max(offset, 0), s.duration) };
}

function pickAt(t: number, hits: SessionSpan[], all: SessionSpan[], cur?: SessionSpan): Target | null {
  if (hits.length) {
    const s = (cur && hits.includes(cur) ? cur : hits[hits.length - 1])!;
    return { sessionId: s.id, offset: t - s.start };
  }
  // dans un trou entre deux sessions : début de la suivante
  const next = [...all].sort((a, b) => a.start - b.start).find((s) => s.start > t);
  if (next) return { sessionId: next.id, offset: 0, note: "aucune session à cette heure, session suivante" };
  return null;
}
