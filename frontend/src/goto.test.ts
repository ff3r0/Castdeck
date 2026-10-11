import { describe, expect, it } from "vitest";
import { parseGoto, resolveGoto, type SessionSpan } from "./goto";
import { absolute, zonedToEpoch } from "./timefmt";

const TZ = "Europe/Paris";
// "maintenant" = 2026-10-11 15:00 Paris
const NOW = zonedToEpoch(2026, 10, 11, 15 * 3600, TZ);
const at = (d: number, h: number, m = 0, s = 0) => zonedToEpoch(2026, 10, d, h * 3600 + m * 60 + s, TZ);

const sessions: SessionSpan[] = [
  { id: "a", start: at(10, 10, 30), duration: 3600 }, // hier 10:30 -> 11:30
  { id: "b", start: at(10, 14, 0), duration: 1800 }, // hier 14:00 -> 14:30
  { id: "c", start: at(11, 9, 0), duration: 7200 }, // aujourd'hui 09:00 -> 11:00
];

describe("parseGoto", () => {
  it("formats", () => {
    expect(parseGoto("11:04")).toEqual({ kind: "clock", day: null, secOfDay: 11 * 3600 + 240, raw: "11:04" });
    expect(parseGoto("+5m")).toEqual({ kind: "relative", seconds: 300 });
    expect(parseGoto("-1h30m")).toEqual({ kind: "relative", seconds: -5400 });
    expect(parseGoto("+90")).toEqual({ kind: "relative", seconds: 90 });
    expect(parseGoto("@00:12:30")).toEqual({ kind: "offset", seconds: 750 });
    expect(parseGoto("hier 11:00")).toMatchObject({ kind: "clock", day: { rel: 1 }, secOfDay: 39600 });
    expect(parseGoto("Hier à 9h")).toMatchObject({ kind: "clock", day: { rel: 1 }, secOfDay: 32400 });
    expect(parseGoto("10/10 11:00")).toMatchObject({ day: { y: null, m: 10, d: 10 } });
    expect(parseGoto("2026-10-10 11:00:05")).toMatchObject({ day: { y: 2026, m: 10, d: 10 }, secOfDay: 39605 });
    expect(parseGoto("n'importe quoi")).toBeNull();
    expect(parseGoto("25:00")).toBeNull();
    expect(parseGoto("+5x")).toBeNull();
  });
});

describe("resolveGoto", () => {
  const go = (s: string, cur: { id: string; offset: number } | null = null) =>
    resolveGoto(parseGoto(s)!, sessions, cur, TZ, NOW);

  it("hier 11:00 traverse les sessions", () => {
    expect(go("hier 11:00", { id: "c", offset: 10 })).toEqual({ sessionId: "a", offset: 1800 });
  });
  it("heure seule : session courante prioritaire, sinon la plus récente", () => {
    expect(go("10:45")).toEqual({ sessionId: "c", offset: 6300 });
    expect(go("14:10")).toEqual({ sessionId: "b", offset: 600 });
  });
  it("trou entre sessions -> début de la suivante", () => {
    expect(go("hier 12:00")).toMatchObject({ sessionId: "b", offset: 0 });
  });
  it("relatif et position", () => {
    expect(go("+5m", { id: "a", offset: 100 })).toEqual({ sessionId: "a", offset: 400 });
    expect(go("-10m", { id: "a", offset: 100 })).toEqual({ sessionId: "a", offset: 0 });
    expect(go("@1:00", { id: "b", offset: 0 })).toEqual({ sessionId: "b", offset: 60 });
    expect(go("00:12:30", { id: "a", offset: 0 })).toEqual({ sessionId: "a", offset: 750 });
  });
});

describe("timefmt", () => {
  it("heure absolue relative", () => {
    expect(absolute(at(10, 11, 4, 12), TZ, NOW)).toBe("hier 11:04:12");
    expect(absolute(at(11, 9, 0, 0), TZ, NOW)).toBe("aujourd'hui 09:00:00");
    expect(absolute(at(6, 8, 0, 0), TZ, NOW)).toBe("mar. 06/10 08:00:00");
  });
});
