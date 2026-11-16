import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "paperclerk",
  description: "Local-first AI clerk for your documents, bills and subscriptions.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <strong>paperclerk</strong>
          <nav>
            <a href="/">Documents</a>
            <span className="muted">Bills (week 3)</span>
            <span className="muted">Organize (week 2)</span>
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
