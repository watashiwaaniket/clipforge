"use client";

import { useEffect, useState } from "react";

type Status = "checking" | "up" | "down";

const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function HealthPill() {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    let cancelled = false;

    const ping = () => {
      fetch(`${api}/api/v1/health`)
        .then((response) => {
          if (!cancelled) setStatus(response.ok ? "up" : "down");
        })
        .catch(() => {
          if (!cancelled) setStatus("down");
        });
    };

    ping();
    const id = window.setInterval(ping, 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const label =
    status === "up" ? "API live" : status === "down" ? "API offline" : "Checking API";
  const dot =
    status === "up" ? "bg-acid" : status === "down" ? "bg-[#ff5d4a]" : "bg-muted";

  return (
    <p
      className="inline-flex h-10 items-center gap-2 rounded-full bg-raise px-3 font-mono text-xs text-muted shadow-card"
      aria-live="polite"
      title={api}
    >
      <span className={`size-2 rounded-full ${dot}`} aria-hidden />
      {label}
    </p>
  );
}
