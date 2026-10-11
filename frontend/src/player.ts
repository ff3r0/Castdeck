// Enveloppe d'asciinema-player qui expose tout en TEMPS D'ORIGINE.
//
// Le serveur ne transmet jamais idle_time_limit au player (sinon il
// compresse seul). Quand "compresser les temps morts" est actif, c'est le
// serveur qui compresse (?idle=) et on convertit avec la TimeMap.
import * as AsciinemaPlayer from "asciinema-player";
import "asciinema-player/dist/bundle/asciinema-player.css";
import { api, type Session } from "./api";
import { TimeMap } from "./timemap";

type Player = ReturnType<typeof AsciinemaPlayer.create>;

export const TERMINAL_FONT =
  "'FiraCode Nerd Font Mono', 'FiraCode Nerd Font', 'Fira Code', 'JetBrainsMono Nerd Font', " +
  "'JetBrains Mono', 'Hack', 'DejaVu Sans Mono', 'Liberation Mono', monospace";

export interface LoadOptions {
  at?: number; // temps d'origine
  play?: boolean;
}

export class PlayerCtl {
  private p: Player | null = null;
  private generation = 0;
  tm = TimeMap.identity();
  session: Session | null = null;
  playing = false;
  speed = 1;
  compress = false;
  onEnded: () => void = () => {};
  onStateChange: () => void = () => {};

  constructor(
    private readonly el: HTMLElement,
    private readonly defaultIdleLimit: number,
  ) {}

  get idleLimit(): number {
    return this.session?.idle_time_limit ?? this.defaultIdleLimit;
  }

  get element(): HTMLElement | null {
    return this.p?.el ?? null;
  }

  async load(s: Session, opts: LoadOptions = {}): Promise<void> {
    const gen = ++this.generation;
    this.dispose();
    this.session = s;
    let url = api.castUrl(s.id);
    if (this.compress) {
      const dto = await api.timemap(s.id, this.idleLimit);
      if (gen !== this.generation) return;
      this.tm = new TimeMap(dto.knots);
      url = api.castUrl(s.id, this.idleLimit);
    } else {
      this.tm = TimeMap.identity();
    }
    // markers passés en option = temps du player (non compressés par lui)
    const markers = s.markers.map(([t, label]) => [this.tm.toDisplay(t), label || "marker"] as [number, string]);
    const p = AsciinemaPlayer.create(url, this.el, {
      fit: "width",
      preload: true,
      autoPlay: false,
      speed: this.speed,
      theme: "exegol",
      terminalFontFamily: TERMINAL_FONT,
      terminalLineHeight: 1.25,
      controls: true,
      markers,
    });
    this.p = p;
    p.addEventListener("play", () => this.setPlaying(true));
    p.addEventListener("playing", () => this.setPlaying(true));
    p.addEventListener("pause", () => this.setPlaying(false));
    p.addEventListener("ended", () => {
      this.setPlaying(false);
      this.onEnded();
    });
    if (opts.at && opts.at > 0) await p.seek(this.tm.toDisplay(opts.at));
    if (gen !== this.generation) return;
    if (opts.play) await p.play();
  }

  /** Recharge (vitesse / compression) en gardant la position et l'état. */
  async reload(): Promise<void> {
    if (!this.session) return;
    await this.load(this.session, { at: this.currentOrig(), play: this.playing });
  }

  private setPlaying(v: boolean) {
    if (this.playing !== v) {
      this.playing = v;
      this.onStateChange();
    }
  }

  dispose(): void {
    if (this.p) {
      this.p.dispose();
      this.p = null;
    }
    this.el.replaceChildren();
    this.setPlaying(false);
  }

  currentOrig(): number {
    if (!this.p) return 0;
    return Math.max(0, this.tm.toOrig(this.p.getCurrentTime() ?? 0));
  }

  /** Durée affichée par le player (compressée ou non). */
  displayDuration(): number {
    const d = this.p?.getDuration();
    if (d) return d;
    return this.session ? this.tm.toDisplay(this.session.duration) : 0;
  }

  async seekOrig(t: number): Promise<void> {
    if (!this.p || !this.session) return;
    const clamped = Math.min(Math.max(t, 0), this.session.duration);
    await this.p.seek(this.tm.toDisplay(clamped));
  }

  async toggle(): Promise<void> {
    if (!this.p) return;
    if (this.playing) await this.p.pause();
    else await this.p.play();
  }

  async play(): Promise<void> {
    await this.p?.play();
  }
}
