import "./style.css";
import { api, type Bookmark, type Command, type Config, type SearchHit, type Session } from "./api";
import { capturePng, copyPng, downloadBlob, screenTextFromDom } from "./capture";
import { $, copyText, debounce, h, toast } from "./dom";
import { parseGoto, resolveGoto } from "./goto";
import { PlayerCtl } from "./player";
import { absolute, clock, duration, relativeDay, resolveTz, zonedToEpoch } from "./timefmt";

const SEEK_STEP = 5;
const CMD_LEAD = 1; // on seek 1 s avant la commande pour voir le prompt

const store = {
  get<T>(k: string, d: T): T {
    try {
      const v = localStorage.getItem(`castdeck.${k}`);
      return v === null ? d : (JSON.parse(v) as T);
    } catch {
      return d;
    }
  },
  set(k: string, v: unknown) {
    try {
      localStorage.setItem(`castdeck.${k}`, JSON.stringify(v));
    } catch {
      /* stockage indisponible */
    }
  },
};

interface TimelineMarker {
  t: number; // temps d'origine
  label: string;
  kind: "auto" | "bookmark";
}

class App {
  cfg!: Config;
  tz = resolveTz(null);
  sessions: Session[] = [];
  byId = new Map<string, Session>();
  current: Session | null = null;
  commands: Command[] = [];
  bookmarks: Bookmark[] = [];
  queue: string[] = store.get<string[]>("queue", []);
  matchCounts = new Map<string, number>();
  currentCmd = -1;
  player!: PlayerCtl;
  private lastStatusPhase = "";

  async init() {
    this.cfg = await api.config();
    this.tz = resolveTz(this.cfg.tz);
    this.player = new PlayerCtl($("#player"), this.cfg.idle_limit);
    this.player.speed = store.get("speed", 1);
    this.player.compress = store.get("compress", false);
    ($("#speed") as HTMLSelectElement).value = String(this.player.speed);
    ($("#compress") as HTMLInputElement).checked = this.player.compress;
    ($("#autonext") as HTMLInputElement).checked = store.get("autonext", true);
    this.player.onEnded = () => this.playNext();
    this.player.onStateChange = () => document.body.classList.toggle("playing", this.player.playing);

    this.bindUi();
    this.bindKeys();
    await this.refreshSessions();
    this.pollStatus();
    window.setInterval(() => this.tick(), 250); // ~4 Hz

    const last = store.get<string | null>("session", null);
    const first = (last && this.byId.get(last)) || this.sessions[this.sessions.length - 1];
    if (first) await this.openSession(first.id, { at: last ? store.get("offset", 0) : 0 });
  }

  // ---------------------------------------------------------------- sessions

  async refreshSessions() {
    this.sessions = await api.sessions();
    this.byId = new Map(this.sessions.map((s) => [s.id, s]));
    this.queue = this.queue.filter((id) => this.byId.has(id));
    if (this.current) this.current = this.byId.get(this.current.id) ?? this.current;
    this.renderSessions();
    this.renderQueue();
  }

  renderSessions() {
    $("#sessions-count").textContent = String(this.sessions.length);
    const ul = $("#sessions");
    ul.replaceChildren(
      ...this.sessions.map((s) => {
        const n = this.matchCounts.get(s.id);
        const inQueue = this.queue.includes(s.id);
        return h(
          "li",
          {
            class: `session${s.id === this.current?.id ? " active" : ""}`,
            title: s.file,
            onclick: () => this.openSession(s.id),
          },
          h(
            "div",
            { class: "row1" },
            h("span", { class: "day" }, relativeDay(s.start, this.tz)),
            h("span", { class: "clock" }, clock(s.start, this.tz, false)),
            n ? h("span", { class: "badge", title: `${n} résultat(s) pour la recherche` }, String(n)) : null,
            h(
              "button",
              {
                class: `icon add${inQueue ? " on" : ""}`,
                title: inQueue ? "Retirer de la file" : "Ajouter à la file de lecture",
                onclick: (e: Event) => {
                  e.stopPropagation();
                  this.toggleQueue(s.id);
                },
              },
              inQueue ? "✓" : "+",
            ),
          ),
          h(
            "div",
            { class: "row2 muted" },
            h("span", {}, duration(s.duration)),
            h("span", {}, `${s.n_commands} cmd`),
            h("span", { class: "file" }, s.file),
          ),
        );
      }),
    );
  }

  async openSession(id: string, opts: { at?: number; play?: boolean } = {}) {
    const s = this.byId.get(id);
    if (!s) return;
    const changed = this.current?.id !== id;
    this.current = s;
    store.set("session", id);
    if (changed) {
      $("#session-name").textContent = s.file;
      $("#session-name").classList.remove("muted");
      const [cmds, bms] = await Promise.all([api.commands(id), api.bookmarks(id)]);
      if (this.current?.id !== id) return;
      this.commands = cmds;
      this.bookmarks = bms;
      this.currentCmd = -1;
      this.renderCommands();
      this.renderBookmarks();
      this.renderSessions();
      this.renderQueue();
      await this.player.load(s, opts);
    } else {
      if (opts.at !== undefined) await this.player.seekOrig(opts.at);
      if (opts.play) await this.player.play();
    }
    this.renderTimeline();
  }

  // ------------------------------------------------------------------ queue

  toggleQueue(id: string) {
    const i = this.queue.indexOf(id);
    if (i >= 0) this.queue.splice(i, 1);
    else this.queue.push(id);
    this.saveQueue();
  }

  saveQueue() {
    store.set("queue", this.queue);
    this.renderQueue();
    this.renderSessions();
  }

  renderQueue() {
    const ol = $("#queue");
    $("#queue-empty").hidden = this.queue.length > 0;
    let dragFrom = -1;
    ol.replaceChildren(
      ...this.queue.map((id, i) => {
        const s = this.byId.get(id)!;
        const li = h(
          "li",
          {
            class: `queued${id === this.current?.id ? " active" : ""}`,
            draggable: "true",
            onclick: () => this.openSession(id, { play: true }),
            ondragstart: (e: Event) => {
              dragFrom = i;
              (e as DragEvent).dataTransfer?.setData("text/plain", id);
              li.classList.add("dragging");
            },
            ondragend: () => li.classList.remove("dragging"),
            ondragover: (e: Event) => {
              e.preventDefault();
              li.classList.add("over");
            },
            ondragleave: () => li.classList.remove("over"),
            ondrop: (e: Event) => {
              e.preventDefault();
              if (dragFrom < 0 || dragFrom === i) return;
              const [moved] = this.queue.splice(dragFrom, 1);
              this.queue.splice(i, 0, moved);
              this.saveQueue();
            },
          },
          h("span", { class: "grip", "aria-hidden": "true" }, "⋮⋮"),
          h("span", { class: "day" }, `${relativeDay(s.start, this.tz)} ${clock(s.start, this.tz, false)}`),
          h("span", { class: "muted" }, duration(s.duration)),
          h(
            "button",
            {
              class: "icon",
              title: "Retirer",
              onclick: (e: Event) => {
                e.stopPropagation();
                this.toggleQueue(id);
              },
            },
            "×",
          ),
        );
        return li;
      }),
    );
  }

  /** Fin de session : suivante de la file, sinon suivante chronologique. */
  playNext() {
    if (!($("#autonext") as HTMLInputElement).checked || !this.current) return;
    let next: string | undefined;
    if (this.queue.length) {
      const i = this.queue.indexOf(this.current.id);
      next = i >= 0 ? this.queue[i + 1] : this.queue[0];
    } else {
      const i = this.sessions.findIndex((s) => s.id === this.current!.id);
      next = this.sessions[i + 1]?.id;
    }
    if (next) {
      toast("Session suivante");
      void this.openSession(next, { play: true });
    }
  }

  // --------------------------------------------------------------- commands

  visibleCommands(): { c: Command; i: number }[] {
    const raw = ($("#cmd-filter") as HTMLInputElement).value.trim().toLowerCase();
    const terms = raw.split(/\s+/).filter(Boolean);
    const ctx = terms.filter((t) => t.startsWith("ctx=")).map((t) => t.slice(4));
    const words = terms.filter((t) => !t.startsWith("ctx="));
    return this.commands
      .map((c, i) => ({ c, i }))
      .filter(
        ({ c }) =>
          (!ctx.length || ctx.includes(c.ctx.toLowerCase())) &&
          words.every((w) => c.cmd.toLowerCase().includes(w)),
      );
  }

  renderCommands() {
    const vis = this.visibleCommands();
    $("#cmd-count").textContent = this.commands.length ? String(this.commands.length) : "";
    const src = $("#cmd-source");
    const heuristic = this.commands.length > 0 && this.commands[0].source === "heuristic";
    src.hidden = !heuristic && this.commands.length > 0;
    src.textContent = heuristic
      ? "Pas de commands.log pour cette session : commandes détectées depuis le prompt (heuristique)."
      : this.current && !this.current.outputs_indexed
        ? "Aucune commande dans commands.log ; détection depuis le prompt en cours…"
        : "Aucune commande pour cette session.";
    $("#commands").replaceChildren(
      ...vis.map(({ c, i }) =>
        h(
          "li",
          {
            class: `cmd${i === this.currentCmd ? " current" : ""}`,
            "data-i": i,
            onclick: () => this.player.seekOrig(c.offset - CMD_LEAD),
          },
          h("span", { class: "time", title: absolute(c.abs_time, this.tz) }, clock(c.abs_time, this.tz)),
          h("span", { class: `ctx${c.ctx === "cli" ? "" : " pane"}` }, c.ctx),
          h("code", {}, c.cmd),
          h(
            "button",
            {
              class: "icon copy",
              title: "Copier la commande",
              onclick: (e: Event) => {
                e.stopPropagation();
                void copyText(c.cmd).then(() => toast("Commande copiée"));
              },
            },
            "⧉",
          ),
        ),
      ),
    );
  }

  updateCurrentCommand(t: number) {
    let idx = -1;
    for (let i = 0; i < this.commands.length; i++) {
      // on se place CMD_LEAD s avant une commande : elle est déjà "courante"
      if (this.commands[i].offset <= t + CMD_LEAD + 0.05) idx = i;
      else break;
    }
    if (idx === this.currentCmd) return;
    this.currentCmd = idx;
    const ul = $("#commands");
    ul.querySelector(".current")?.classList.remove("current");
    const li = ul.querySelector<HTMLElement>(`[data-i="${idx}"]`);
    if (li) {
      li.classList.add("current");
      if (!ul.matches(":hover")) li.scrollIntoView({ block: "nearest" });
    }
  }

  // -------------------------------------------------------------- bookmarks

  renderBookmarks() {
    $("#bm-count").textContent = this.bookmarks.length ? String(this.bookmarks.length) : "";
    const s = this.current;
    $("#bookmarks").replaceChildren(
      ...(this.bookmarks.length
        ? this.bookmarks.map((b, i) =>
            h(
              "li",
              { class: "bm", onclick: () => this.player.seekOrig(b.offset) },
              h("span", { class: "time" }, s ? clock(s.start + b.offset, this.tz) : duration(b.offset)),
              h("span", { class: "label" }, b.label || "(sans label)"),
              h(
                "button",
                {
                  class: "icon",
                  title: "Supprimer",
                  onclick: (e: Event) => {
                    e.stopPropagation();
                    const next = this.bookmarks.filter((_, j) => j !== i);
                    void this.saveBookmarks(next);
                  },
                },
                "×",
              ),
            ),
          )
        : [h("li", { class: "muted hint" }, "Aucun bookmark. Bouton « Bookmark ici » ou touche b.")]),
    );
  }

  async saveBookmarks(items: Bookmark[]) {
    if (!this.current) return;
    this.bookmarks = await api.saveBookmarks(this.current.id, items);
    this.renderBookmarks();
    this.renderTimeline();
  }

  startBookmark() {
    if (!this.current) return;
    const input = $("#bookmark-label") as HTMLInputElement;
    input.dataset.offset = String(this.player.currentOrig());
    input.hidden = false;
    input.value = "";
    input.focus();
  }

  // --------------------------------------------------------------- timeline

  markers(): TimelineMarker[] {
    if (!this.current) return [];
    return [
      ...this.current.markers.map(([t, label]) => ({ t, label: label || "marker", kind: "auto" as const })),
      ...this.bookmarks.map((b) => ({ t: b.offset, label: b.label || "bookmark", kind: "bookmark" as const })),
    ].sort((a, b) => a.t - b.t);
  }

  renderTimeline() {
    const track = $("#timeline .track");
    const s = this.current;
    if (!s) return track.replaceChildren();
    const total = this.player.tm.toDisplay(s.duration) || 1;
    const pos = (t: number) => `${(this.player.tm.toDisplay(t) / total) * 100}%`;
    const tip = (el: HTMLElement, text: string) => {
      el.addEventListener("mouseenter", () => this.showTip(el, text));
      el.addEventListener("mouseleave", () => this.hideTip());
    };
    const items: HTMLElement[] = [];
    this.commands.forEach((c) => {
      const el = h("div", {
        class: "tick",
        style: `left:${pos(c.offset)}`,
        onclick: (e: Event) => {
          e.stopPropagation();
          void this.player.seekOrig(c.offset - CMD_LEAD);
        },
      });
      tip(el, `${clock(c.abs_time, this.tz)}  ${c.cmd}`);
      items.push(el);
    });
    this.markers().forEach((m) => {
      const el = h("div", {
        class: `marker ${m.kind}`,
        style: `left:${pos(m.t)}`,
        onclick: (e: Event) => {
          e.stopPropagation();
          void this.player.seekOrig(m.t);
        },
      });
      tip(el, `${m.kind === "bookmark" ? "★" : "◆"} ${clock(s.start + m.t, this.tz)}  ${m.label}`);
      items.push(el);
    });
    track.replaceChildren(...items);
  }

  showTip(anchor: HTMLElement, text: string) {
    const tipEl = $("#tooltip");
    tipEl.textContent = text;
    tipEl.hidden = false;
    const r = anchor.getBoundingClientRect();
    const w = tipEl.offsetWidth;
    tipEl.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8)}px`;
    tipEl.style.top = `${r.top - tipEl.offsetHeight - 6}px`;
  }

  hideTip() {
    $("#tooltip").hidden = true;
  }

  // ------------------------------------------------------------------- tick

  private lastSaved = 0;

  tick() {
    const s = this.current;
    if (!s) return;
    const t = this.player.currentOrig();
    $("#abs-time").textContent = absolute(s.start + t, this.tz);
    const total = this.player.tm.toDisplay(s.duration) || 1;
    ($("#timeline .playhead") as HTMLElement).style.left = `${(this.player.tm.toDisplay(t) / total) * 100}%`;
    this.updateCurrentCommand(t);
    if (Math.abs(t - this.lastSaved) > 2) {
      this.lastSaved = t;
      store.set("offset", t);
    }
  }

  // ------------------------------------------------------------- recherche

  private searchSeq = 0;

  rangeEpoch(id: string): number | undefined {
    const v = ($(id) as HTMLInputElement).value; // "2026-10-10T11:00"
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v);
    if (!m) return undefined;
    return zonedToEpoch(+m[1], +m[2], +m[3], +m[4] * 3600 + +m[5] * 60, this.tz);
  }

  async runSearch() {
    const q = ($("#q") as HTMLInputElement).value.trim();
    const seq = ++this.searchSeq;
    const panel = $("#results");
    if (!q) {
      this.matchCounts.clear();
      this.renderSessions();
      panel.hidden = true;
      return;
    }
    const hits = await api.search(q, {
      kind: ($("#q-kind") as HTMLSelectElement).value,
      from: this.rangeEpoch("#q-from"),
      to: this.rangeEpoch("#q-to"),
    });
    if (seq !== this.searchSeq) return;
    this.matchCounts.clear();
    for (const hit of hits) {
      if (hit.session_id) this.matchCounts.set(hit.session_id, (this.matchCounts.get(hit.session_id) ?? 0) + 1);
    }
    this.renderSessions();
    this.renderResults(hits);
  }

  renderResults(hits: SearchHit[]) {
    const panel = $("#results");
    panel.hidden = false;
    const groups = new Map<string, SearchHit[]>();
    for (const hit of hits) {
      const key = hit.kind === "pane" ? `pane:${hit.pane}` : hit.session_id ?? "orphan";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(hit);
    }
    const order = [...groups.keys()].sort((a, b) => {
      const sa = this.byId.get(a)?.start ?? Infinity;
      const sb = this.byId.get(b)?.start ?? Infinity;
      return sa - sb;
    });
    const status = this.lastStatusPhase !== "ready" && this.cfg.outputs_available
      ? h("p", { class: "muted hint" }, "Indexation des sorties en cours : résultats partiels.")
      : null;
    panel.replaceChildren(
      h(
        "div",
        { class: "results-head" },
        h("span", {}, hits.length ? `${hits.length} résultat(s)` : "Aucun résultat"),
        h("button", { class: "icon", title: "Fermer (Échap)", onclick: () => (panel.hidden = true) }, "×"),
      ),
      ...(status ? [status] : []),
      ...order.map((key) => {
        const list = groups.get(key)!;
        const s = this.byId.get(key);
        const title = s
          ? `${relativeDay(s.start, this.tz)} ${clock(s.start, this.tz, false)} — ${s.file}`
          : key.startsWith("pane:")
            ? `Pane tmux ${key.slice(5)}`
            : "Hors session (commands.log)";
        return h(
          "section",
          { class: "group" },
          h("h3", {}, title),
          ...list.map((hit) => {
            const seekable = hit.session_id !== null && hit.offset !== null;
            const when =
              hit.abs_time !== null ? clock(hit.abs_time, this.tz) : hit.kind === "pane" ? "—" : "";
            const note =
              hit.kind === "pane" ? `trouvé dans pane ${hit.pane}, pas de position temporelle` : "";
            return h(
              "div",
              {
                class: `hit ${hit.kind}${seekable ? " seekable" : ""}`,
                onclick: seekable
                  ? () => {
                      panel.hidden = true;
                      void this.openSession(hit.session_id!, { at: Math.max(0, hit.offset! - CMD_LEAD) });
                    }
                  : undefined,
              },
              h("span", { class: "time" }, when),
              h("span", { class: `kind ${hit.kind}` }, hit.kind === "cmd" ? "cmd" : hit.kind === "pane" ? "pane" : "sortie"),
              h("pre", { html: hit.snippet }),
              note ? h("span", { class: "note muted" }, note) : null,
            );
          }),
        );
      }),
    );
  }

  // ----------------------------------------------------------------- statut

  async pollStatus() {
    try {
      const st = await api.status();
      const el = $("#status");
      if (st.phase === "ready") {
        el.textContent = st.error ? "⚠ sorties non indexées" : `✓ ${st.total} session(s) indexée(s)`;
        el.title = st.error ?? "Index à jour";
      } else {
        el.textContent = `indexation des sorties ${st.done}/${st.total}…`;
        el.title = st.current ?? "";
      }
      if (st.phase === "ready" && this.lastStatusPhase && this.lastStatusPhase !== "ready") {
        // l'heuristique a pu ajouter des commandes : on rafraîchit
        this.lastStatusPhase = st.phase;
        await this.refreshSessions();
        if (($("#q") as HTMLInputElement).value.trim()) await this.runSearch();
        if (this.current) {
          this.commands = await api.commands(this.current.id);
          this.currentCmd = -1;
          this.renderCommands();
          this.renderTimeline();
        }
      }
      this.lastStatusPhase = st.phase;
      if (st.phase !== "ready") window.setTimeout(() => this.pollStatus(), 1500);
    } catch {
      window.setTimeout(() => this.pollStatus(), 5000);
    }
  }

  // ------------------------------------------------------------ actions UI

  async capture(copy = false) {
    const el = this.player.element;
    if (!el || !this.current) return;
    try {
      const blob = await capturePng(el);
      if (copy) {
        await copyPng(blob);
        toast("Capture copiée dans le presse-papier");
      } else {
        const t = clock(this.current.start + this.player.currentOrig(), this.tz).replace(/:/g, "");
        const base = this.current.file.split("/").pop()!.replace(/\.(cast|asciinema)$/, "");
        downloadBlob(blob, `${base}_${t}.png`);
        toast("Capture enregistrée");
      }
    } catch (e) {
      toast(`Capture impossible : ${(e as Error).message}`, "error");
    }
  }

  async copyScreen() {
    if (!this.current) return;
    let text = this.player.element ? screenTextFromDom(this.player.element) : null;
    if (text === null) text = (await api.screen(this.current.id, this.player.currentOrig())).text;
    await copyText(text);
    toast("Texte de l'écran copié");
  }

  async goto(input: string) {
    const g = parseGoto(input);
    if (!g) return toast("Format non reconnu (ex. 11:04, hier 11:00, +5m, @00:12:30)", "error");
    const cur = this.current ? { id: this.current.id, offset: this.player.currentOrig() } : null;
    const target = resolveGoto(g, this.sessions, cur, this.tz);
    if (!target) return toast("Aucune session à ce moment-là", "error");
    if (target.note) toast(target.note);
    await this.openSession(target.sessionId, { at: target.offset });
  }

  seekCommand(dir: 1 | -1) {
    if (!this.commands.length) return;
    const t = this.player.currentOrig() + CMD_LEAD; // on est en général 1 s avant la commande courante
    const list = this.commands;
    const target =
      dir > 0
        ? list.find((c) => c.offset > t + 0.2)
        : [...list].reverse().find((c) => c.offset < t - 0.2);
    if (target) void this.player.seekOrig(target.offset - CMD_LEAD);
  }

  seekMarker(dir: 1 | -1) {
    const t = this.player.currentOrig();
    const ms = this.markers();
    const m = dir > 0 ? ms.find((x) => x.t > t + 0.2) : [...ms].reverse().find((x) => x.t < t - 0.2);
    if (m) void this.player.seekOrig(m.t);
  }

  toggleFullscreen() {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void $("#stage").requestFullscreen?.();
  }

  bindUi() {
    $("#speed").addEventListener("change", (e) => {
      this.player.speed = Number((e.target as HTMLSelectElement).value);
      store.set("speed", this.player.speed);
      void this.player.reload();
    });
    $("#compress").addEventListener("change", async (e) => {
      this.player.compress = (e.target as HTMLInputElement).checked;
      store.set("compress", this.player.compress);
      await this.player.reload();
      this.renderTimeline();
    });
    $("#autonext").addEventListener("change", (e) => store.set("autonext", (e.target as HTMLInputElement).checked));

    const search = debounce(() => void this.runSearch(), 200);
    $("#q").addEventListener("input", search);
    $("#q-kind").addEventListener("change", search);
    $("#q-from").addEventListener("change", search);
    $("#q-to").addEventListener("change", search);
    $("#q").addEventListener("focus", () => {
      if (($("#q") as HTMLInputElement).value.trim()) $("#results").hidden = false;
    });
    $("#search").addEventListener("submit", (e) => {
      e.preventDefault();
      void this.runSearch();
    });
    document.addEventListener("mousedown", (e) => {
      const t = e.target as Node;
      if (!$("#results").contains(t) && !$("#search").contains(t)) $("#results").hidden = true;
    });

    $("#goto").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("#goto-input") as HTMLInputElement;
      void this.goto(input.value);
      input.blur();
    });

    $("#btn-capture").addEventListener("click", () => void this.capture(false));
    $("#btn-capture-copy").addEventListener("click", () => void this.capture(true));
    $("#btn-copy-screen").addEventListener("click", () => void this.copyScreen());
    $("#btn-bookmark").addEventListener("click", () => this.startBookmark());
    $("#bookmark-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("#bookmark-label") as HTMLInputElement;
      const offset = Number(input.dataset.offset ?? this.player.currentOrig());
      input.hidden = true;
      input.blur();
      void this.saveBookmarks([...this.bookmarks, { offset, label: input.value.trim() }]).then(() =>
        toast("Bookmark ajouté"),
      );
    });
    $("#bookmark-label").addEventListener("blur", () => {
      // laisse le temps au submit de passer
      window.setTimeout(() => ($("#bookmark-label").hidden = true), 150);
    });

    $("#cmd-filter").addEventListener("input", () => {
      this.currentCmd = -1;
      this.renderCommands();
    });
    $("#btn-copy-cmds").addEventListener("click", () => {
      const text = this.visibleCommands().map(({ c }) => c.cmd).join("\n");
      if (!text) return toast("Aucune commande visible");
      void copyText(text).then(() => toast(`${this.visibleCommands().length} commande(s) copiée(s)`));
    });

    document.querySelectorAll<HTMLButtonElement>(".tab").forEach((tab) =>
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
        $("#tab-commands").hidden = tab.dataset.tab !== "commands";
        $("#tab-bookmarks").hidden = tab.dataset.tab !== "bookmarks";
      }),
    );

    $("#timeline").addEventListener("click", (e) => {
      const s = this.current;
      if (!s) return;
      const r = $("#timeline").getBoundingClientRect();
      const frac = Math.min(Math.max(((e as MouseEvent).clientX - r.left) / r.width, 0), 1);
      const total = this.player.tm.toDisplay(s.duration);
      void this.player.seekOrig(this.player.tm.toOrig(frac * total));
    });
  }

  bindKeys() {
    // Phase de capture : on gère les raccourcis avant le player (évite les
    // doubles actions quand il a le focus).
    document.addEventListener(
      "keydown",
      (e) => {
        const target = e.target as HTMLElement;
        const typing = target.matches("input, textarea, select") && !(target as HTMLInputElement).readOnly;
        if (e.key === "Escape") {
          $("#results").hidden = true;
          if (typing) target.blur();
          return;
        }
        if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
        let handled = true;
        switch (e.key) {
          case " ":
            void this.player.toggle();
            break;
          case "ArrowLeft":
            if (e.shiftKey) this.seekCommand(-1);
            else void this.player.seekOrig(this.player.currentOrig() - SEEK_STEP);
            break;
          case "ArrowRight":
            if (e.shiftKey) this.seekCommand(1);
            else void this.player.seekOrig(this.player.currentOrig() + SEEK_STEP);
            break;
          case "]":
            this.seekMarker(1);
            break;
          case "[":
            this.seekMarker(-1);
            break;
          case "/":
            ($("#q") as HTMLInputElement).focus();
            ($("#q") as HTMLInputElement).select();
            break;
          case "b":
            this.startBookmark();
            break;
          case "c":
            void this.capture(false);
            break;
          case "f":
            this.toggleFullscreen();
            break;
          default:
            handled = false;
        }
        if (handled) {
          e.preventDefault();
          e.stopPropagation();
        }
      },
      true,
    );
  }
}

const app = new App();
(window as unknown as { castdeck: App }).castdeck = app; // debug / tests e2e
app.init().catch((e) => {
  console.error(e);
  toast(`Erreur de chargement : ${(e as Error).message}`, "error");
});
