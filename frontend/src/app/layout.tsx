import type { Metadata } from "next";
import "./globals.css";
import NavGate from "@/components/NavGate";
import Toaster from "@/components/Toast";
import Providers from "./providers";

export const metadata: Metadata = {
  manifest: "/manifest.json",
  title: "Studeal: never overpay again",
  description: "Autonomous agents that hunt marketplace deals for you across Kijiji, eBay, Craigslist and more, alerting you the moment something matches.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <NavGate />
          <Toaster />
          {children}
        </Providers>
      </body>
    </html>
  );
}
