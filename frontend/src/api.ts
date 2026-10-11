export interface Session {
  id: string;
  file: string;
  version: number;
  start: number;
  start_iso: string;
  duration: number;
  cols: number;
  rows: number;
  n_events: number;
  n_commands: number;
  idle_time_limit: number | null;
  markers: [number, string][];
  outputs_indexed: boolean;
}

export interface Command {
  offset: number;
  abs_time: number;
  ctx: string;
  cmd: string;
  source: "commands.log" | "heuristic";
}

export interface Bookmark {
  offset: number;
  label: string;
  created?: number;
}

export interface SearchHit {
  session_id: string | null;
  offset: number | null;
  abs_time: number | null;
  kind: "cmd" | "output" | "pane";
  snippet: string; // HTML échappé côté serveur, avec <mark>
  pane?: string;
  source?: string;
}

export interface Config {
  version: string;
  tz: string | null;
  redact: boolean;
  idle_limit: number;
  root: string;
  outputs_available: boolean;
}

export interface Status {
  phase: "init" | "pending" | "indexing" | "ready" | "error";
  total: number;
  done: number;
  current: string | null;
  error: string | null;
}

export interface TimeMapDto {
  limit: number;
  knots: [number, number][];
  display_duration: number;
}

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} : ${r.status}`);
  return r.json() as Promise<T>;
}

export const api = {
  config: () => getJson<Config>("/api/config"),
  status: () => getJson<Status>("/api/status"),
  sessions: () => getJson<Session[]>("/api/sessions"),
  commands: (id: string) => getJson<Command[]>(`/api/sessions/${id}/commands`),
  bookmarks: (id: string) => getJson<Bookmark[]>(`/api/sessions/${id}/bookmarks`),
  timemap: (id: string, idle: number) => getJson<TimeMapDto>(`/api/sessions/${id}/timemap?idle=${idle}`),
  screen: (id: string, t: number) => getJson<{ text: string }>(`/api/sessions/${id}/screen?t=${t}`),
  castUrl: (id: string, idle?: number) => `/api/sessions/${id}/cast${idle ? `?idle=${idle}` : ""}`,
  async saveBookmarks(id: string, items: Bookmark[]): Promise<Bookmark[]> {
    const r = await fetch(`/api/sessions/${id}/bookmarks`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(items),
    });
    if (!r.ok) throw new Error(`bookmarks : ${r.status}`);
    return r.json();
  },
  search(q: string, opts: { kind?: string; session?: string; from?: number; to?: number } = {}) {
    const p = new URLSearchParams({ q, kind: opts.kind ?? "all" });
    if (opts.session) p.set("session", opts.session);
    if (opts.from !== undefined) p.set("from", String(opts.from));
    if (opts.to !== undefined) p.set("to", String(opts.to));
    return getJson<SearchHit[]>(`/api/search?${p}`);
  },
};
