import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "paperclerk",
  description: "Local-first AI clerk for your documents, bills and subscriptions.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-white font-sans text-zinc-900 antialiased">
        <header className="flex items-center gap-6 border-b border-zinc-200 px-6 py-3">
          <strong className="text-base">paperclerk</strong>
          <nav className="flex gap-4 text-sm">
            <Link href="/" className="text-blue-600 hover:underline">
              Documents
            </Link>
            <span className="text-zinc-400">Bills (soon)</span>
            <span className="text-zinc-400">Organize (soon)</span>
          </nav>
        </header>
        <main className="mx-auto max-w-5xl p-6">{children}</main>
      </body>
    </html>
  );
}
