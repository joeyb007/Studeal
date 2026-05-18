import type { Metadata } from "next";
import { Fraunces } from "next/font/google";
import "./globals.css";
import Providers from "./providers";

const fraunces = Fraunces({
  subsets: ["latin"],
  variable: "--font-fraunces",
  style: ["italic"],
  weight: ["400", "700"],
});

export const metadata: Metadata = {
  title: "Studeal — Never overpay again",
  description: "AI-powered deal hunting for students. Deploy an agent, get alerted the moment prices drop.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={fraunces.variable}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
