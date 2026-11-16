"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type DocumentOut, type ScanOut } from "@/lib/api";

export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [lastScan, setLastScan] = useState<ScanOut | null>(null);

  const load = useCallback(async () => {
    try {
      setDocs(await api.documents());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function scan() {
    setScanning(true);
    try {
      setLastScan(await api.scan());
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  }

  return (
    <section>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Documents</h1>
        <button
          onClick={scan}
          disabled={scanning}
          className="rounded-md bg-blue-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-default disabled:opacity-60"
        >
          {scanning ? "Scanning…" : "Scan folders"}
        </button>
      </div>

      {lastScan && (
        <p className="mt-2 text-sm text-zinc-500">
          Last scan: {lastScan.new} new, {lastScan.updated} updated, {lastScan.skipped.length}{" "}
          not parsed yet.
        </p>
      )}
      {error && (
        <p className="mt-2 text-sm text-red-600">
          Can&apos;t reach the backend ({error}). Is{" "}
          <code className="rounded bg-zinc-100 px-1">paperclerk serve</code> running?
        </p>
      )}

      <table className="mt-4 w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-left">
            <th className="p-2 font-semibold">ID</th>
            <th className="p-2 font-semibold">File</th>
            <th className="p-2 font-semibold">Source</th>
            <th className="p-2 font-semibold">Type</th>
          </tr>
        </thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id} className="border-b border-zinc-100 hover:bg-zinc-50">
              <td className="p-2 tabular-nums">{d.id}</td>
              <td className="p-2" title={d.path ?? ""}>
                {d.path?.split("/").pop() ?? "—"}
              </td>
              <td className="p-2">{d.source}</td>
              <td className="p-2 text-zinc-500">{d.mime_type ?? "—"}</td>
            </tr>
          ))}
          {docs.length === 0 && !error && (
            <tr>
              <td colSpan={4} className="p-2 text-zinc-500">
                No documents yet. Add a folder to config.toml and press Scan.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
