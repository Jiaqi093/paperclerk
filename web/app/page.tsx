"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type DocumentOut, type ScanOut } from "@/lib/api";

const fileName = (path: string) => path.split("/").filter(Boolean).pop() ?? path;

function Status({ doc }: { doc: DocumentOut }) {
  if (doc.parse_status === "failed") {
    return <span className="text-red-600">⚠ {doc.parse_error ?? "couldn't parse"}</span>;
  }
  if (doc.parse_status === "unsupported") return <span className="text-zinc-500">Not supported</span>;
  if (doc.parse_status === null) return <span className="text-zinc-500">Not parsed yet</span>;
  if (doc.needs_vision) {
    return (
      <span className="text-amber-700" title="Scanned or a photo: read by the vision model later">
        Needs vision
      </span>
    );
  }
  return <span className="text-green-700">✓</span>;
}

function ScanSummary({ scan }: { scan: ScanOut }) {
  const parts = [
    `${scan.new} new`,
    `${scan.updated} updated`,
    `${scan.removed} removed`,
    scan.cloud_only.length > 0 && `${scan.cloud_only.length} in the cloud (not downloaded)`,
    scan.skipped.length > 0 && `${scan.skipped.length} not supported`,
  ].filter(Boolean);
  return (
    <div className="mt-2 text-sm text-zinc-500">
      <p>Last scan: {parts.join(" · ")}</p>
      {scan.failed.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-red-600">
            {scan.failed.length} couldn&apos;t be read or parsed
          </summary>
          <ul className="mt-1 space-y-0.5 pl-4">
            {scan.failed.map((path) => (
              <li key={path} title={path}>
                <span className="font-medium text-zinc-700">{fileName(path)}</span> —{" "}
                {scan.reasons[path] ?? "unknown error"}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [lastScan, setLastScan] = useState<ScanOut | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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

  // While a scan runs the list can still be read: refresh it so documents appear as found.
  // One poll at a time, and a failed poll doesn't touch the error shown (the scan owns it).
  const polling = useRef(false);
  useEffect(() => {
    if (!scanning) return;
    const timer = setInterval(async () => {
      if (polling.current) return;
      polling.current = true;
      try {
        setDocs(await api.documents());
      } catch {
        // The scan request reports problems; keep the list as it was.
      } finally {
        polling.current = false;
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [scanning]);

  async function scan() {
    setScanning(true);
    setNotice(null);
    try {
      setLastScan(await api.scan());
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice("A scan is already running (started elsewhere). Try again when it finishes.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
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

      {notice && <p className="mt-2 text-sm text-amber-700">{notice}</p>}
      {lastScan && <ScanSummary scan={lastScan} />}
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
            <th className="p-2 font-semibold">Status</th>
          </tr>
        </thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id} className="border-b border-zinc-100 hover:bg-zinc-50">
              <td className="p-2 tabular-nums">{d.id}</td>
              <td className="p-2" title={d.path ?? ""}>
                {d.path ? fileName(d.path) : "—"}
              </td>
              <td className="p-2">{d.source}</td>
              <td className="p-2 text-zinc-500">{d.mime_type ?? "—"}</td>
              <td className="p-2">
                <Status doc={d} />
              </td>
            </tr>
          ))}
          {docs.length === 0 && !error && (
            <tr>
              <td colSpan={5} className="p-2 text-zinc-500">
                No documents yet. Add a folder to config.toml and press Scan.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
