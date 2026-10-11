// Correspondance temps d'origine <-> temps du player quand les temps morts
// sont compressés. Même algorithme que castdeck/asciicast.py (time_knots) :
// fonction affine par morceaux, pente 1 ou 0 (paliers), pente 1 après le
// dernier point. Les offsets du backend sont TOUJOURS en temps d'origine.

export type Knot = [orig: number, display: number];

export class TimeMap {
  constructor(readonly knots: Knot[]) {}

  static identity(): TimeMap {
    return new TimeMap([[0, 0]]);
  }

  toDisplay(x: number): number {
    const k = this.knots;
    if (x <= k[0][0]) return x;
    let lo = 0;
    let hi = k.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (k[mid][0] <= x) lo = mid;
      else hi = mid - 1;
    }
    const [o, d] = k[lo];
    if (lo + 1 < k.length && k[lo + 1][1] === d) return d; // palier
    return d + (x - o);
  }

  // Sur un palier, renvoie la fin du palier : c'est l'event que joue le player.
  toOrig(y: number): number {
    const k = this.knots;
    if (y <= 0) return y;
    let best = 0;
    for (let i = 0; i < k.length; i++) {
      if (k[i][1] <= y) best = i;
      else break;
    }
    while (best + 1 < k.length && k[best + 1][1] === k[best][1]) best++;
    const [o, d] = k[best];
    return o + (y - d);
  }
}
