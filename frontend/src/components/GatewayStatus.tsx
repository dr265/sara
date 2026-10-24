"use client";

import { useEffect, useState } from "react";
import { LEGACY_API_URL, saraFetch } from "@/lib/api";
import { StatusPill } from "@/lib/ui";

export default function GatewayStatus() {
  const [status, setStatus] = useState<"checking" | "online" | "unavailable">("checking");
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | undefined;
    async function check() {
      if (document.hidden) return;
      controller?.abort();
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 8000);
      try {
        const response = await saraFetch(`${LEGACY_API_URL}/api/health`, { signal: controller.signal, cache: "no-store" });
        const data: unknown = await response.json();
        const online = response.ok && typeof data === "object" && data !== null && "status" in data && data.status === "online";
        if (!disposed) setStatus(online ? "online" : "unavailable");
      } catch { if (!disposed) setStatus("unavailable"); }
      finally { window.clearTimeout(timeout); }
    }
    void check();
    const interval = window.setInterval(check, 30000);
    document.addEventListener("visibilitychange", check);
    return () => { disposed = true; controller?.abort(); window.clearInterval(interval); document.removeEventListener("visibilitychange", check); };
  }, []);
  return <span role="status" title="Backend health check; individual integrations may have a different status"><StatusPill tone={status === "online" ? "ok" : status === "checking" ? "idle" : "warn"}>{status === "checking" ? "Checking backend" : status === "online" ? "Backend connected" : "Backend unavailable"}</StatusPill></span>;
}
