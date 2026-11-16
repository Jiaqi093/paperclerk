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
      <div className="row">
        <h1>Documents</h1>
        <button onClick={scan} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan folders"}
        </button>
      </div>

      {lastScan && (
        <p className="muted">
          Last scan: {lastScan.new} new, {lastScan.updated} updated, {lastScan.skipped.length}{" "}
          not parsed yet.
        </p>
      )}
      {error && (
        <p className="error">
          Can&apos;t reach the backend ({error}). Is <code>paperclerk serve</code> running?
        </p>
      )}

      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>File</th>
            <th>Source</th>
            <th>Type</th>
          </tr>
        </thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id}>
              <td>{d.id}</td>
              <td title={d.path ?? ""}>{d.path?.split("/").pop() ?? "—"}</td>
              <td>{d.source}</td>
              <td>{d.mime_type ?? "—"}</td>
            </tr>
          ))}
          {docs.length === 0 && !error && (
            <tr>
              <td colSpan={4} className="muted">
                No documents yet. Add a folder to config.toml and press Scan.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
