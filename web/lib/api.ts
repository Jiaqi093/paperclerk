// Typed client for the FastAPI backend (src/paperclerk/api/app.py). Keep in sync with its models.

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type Health = { status: string; version: string };

export type DocumentOut = {
  id: number;
  path: string | null;
  source: "local" | "gmail";
  mime_type: string | null;
  size_bytes: number | null;
  created_at: string;
};

export type ScanOut = { seen: number; new: number; updated: number; skipped: string[] };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store", ...init });
  if (!res.ok) throw new Error(`${init?.method ?? "GET"} ${path} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/api/health"),
  documents: (limit = 100) => request<DocumentOut[]>(`/api/documents?limit=${limit}`),
  scan: () => request<ScanOut>("/api/scan", { method: "POST" }),
};
