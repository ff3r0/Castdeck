import { describe, expect, it } from "vitest";
import cases from "../../tests/fixtures/timemap_cases.json";
import { TimeMap, type Knot } from "./timemap";

// Cas générés par tests/test_timemap.py : le front doit convertir exactement
// comme le backend (piège idleTimeLimit).
describe("TimeMap = implémentation Python", () => {
  for (const [i, c] of cases.entries()) {
    it(`cas ${i} (limit=${c.limit})`, () => {
      const tm = new TimeMap(c.knots as Knot[]);
      c.probes.forEach((p, j) => {
        expect(tm.toDisplay(p)).toBeCloseTo(c.display[j], 9);
        expect(tm.toOrig(tm.toDisplay(p))).toBeCloseTo(c.back[j], 9);
      });
    });
  }

  it("identité sans compression", () => {
    const tm = TimeMap.identity();
    expect(tm.toDisplay(42.5)).toBe(42.5);
    expect(tm.toOrig(42.5)).toBe(42.5);
  });
});
