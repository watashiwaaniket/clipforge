"use client";

import Link from "next/link";
import { signOut, useSession } from "next-auth/react";

import { HealthPill } from "./health-pill";

export function WorkspaceHeader({
  showDashboardLink = false,
}: {
  showDashboardLink?: boolean;
}) {
  const { data: session } = useSession();

  return (
    <header className="flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-center gap-4">
        <Link href="/" className="inline-flex h-10 items-center font-medium tracking-tight">
          ClipForge
        </Link>
        {showDashboardLink ? (
          <Link
            href="/dashboard"
            className="inline-flex h-10 items-center rounded-full bg-raise px-4 text-sm text-muted shadow-card transition-colors hover:text-ink"
          >
            ← Dashboard
          </Link>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <HealthPill />
        {session?.user?.email ? (
          <span className="hidden h-10 items-center rounded-full bg-raise px-3.5 font-mono text-xs text-muted shadow-card sm:inline-flex">
            {session.user.email}
          </span>
        ) : null}
        <button
          type="button"
          onClick={() => signOut({ callbackUrl: "/" })}
          className="inline-flex h-10 items-center rounded-full bg-raise px-4 text-sm text-ink shadow-card transition-colors hover:text-acid"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
