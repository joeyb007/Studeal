import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Studeal — Never overpay again",
  description: "AI-powered deal hunting for students. Set a watchlist, get daily alerts when prices drop.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
