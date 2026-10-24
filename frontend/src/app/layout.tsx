import type { Metadata } from "next";
import { Inter, Instrument_Serif, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";
import CommandCenter from "@/components/CommandCenter";
import CinematicBackground from "@/components/CinematicBackground";
import AuthTokenBridge from "@/components/AuthTokenBridge";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});
const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-instrument",
  display: "swap",
});
const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
});

export const metadata: Metadata = {
  title: "SARA AI | Intelligent Workspace",
  description: "AI Agent Operating Platform",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <ClerkProvider>
      <html lang="en" className="dark">
        <head>
          <script
            type="importmap"
            dangerouslySetInnerHTML={{
              __html: JSON.stringify({
                imports: {
                  "three": "https://unpkg.com/three@0.185.1/build/three.module.js",
                  "three/examples/jsm/": "https://unpkg.com/three@0.185.1/examples/jsm/"
                }
              })
            }}
          />
        </head>
        <body
          className={`${inter.variable} ${instrumentSerif.variable} ${jetbrainsMono.variable} text-white`}
        >
          <CinematicBackground />
          <div className="relative min-h-screen">{children}</div>
          <AuthTokenBridge />
          <CommandCenter />
        </body>
      </html>
    </ClerkProvider>
  );
}
