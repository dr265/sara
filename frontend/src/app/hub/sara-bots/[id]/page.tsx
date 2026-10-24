"use client";

import React from "react";
import CustomBots from "../custom-bots";
import { SB_CSS } from "../sb-css";

export default function BotDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = React.use(params);
  return (
    <main className="relative z-10 min-h-screen w-full bg-transparent text-white pb-24">
      <style>{SB_CSS}</style>
      <CustomBots initialBotId={id} />
    </main>
  );
}
