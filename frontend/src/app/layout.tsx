import type { Metadata } from "next";
import "./globals.css";
import Providers from "./providers";

export const metadata: Metadata = {
  manifest: "/manifest.json",
  title: "Studeal — Never overpay again",
  description: "Autonomous agents that hunt marketplace deals for you — Kijiji, eBay, Craigslist — and alert you the moment something matches.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
