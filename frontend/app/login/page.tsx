import Link from "next/link";
import { Suspense } from "react";

import { isGuestAuthEnabled } from "../../lib/auth-mode";
import { LoginForm } from "./login-form";

export const dynamic = "force-dynamic";

export default async function LoginPage() {
  const guestEnabled = await isGuestAuthEnabled();

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 pb-16 pt-6 sm:px-8">
      <header className="flex items-center justify-between">
        <Link href="/" className="inline-flex h-10 items-center font-medium tracking-tight">
          ClipForge
        </Link>
        <Link
          href="/register"
          className="inline-flex h-10 items-center rounded-full bg-raise px-4 text-sm text-ink shadow-card transition-colors hover:text-acid"
        >
          Create account
        </Link>
      </header>

      <section className="mx-auto mt-16 w-full max-w-md rounded-[28px] bg-raise p-2 shadow-card">
        <div className="rounded-[20px] px-5 py-6 sm:px-6">
          <p className="font-mono text-xs uppercase tracking-[0.16em] text-acid">Sign in</p>
          <h1 className="mt-2 text-2xl font-medium tracking-tight">Welcome back</h1>
          <p className="mt-1 text-sm text-muted">
            {guestEnabled
              ? "Sign in with email and password, or skip sign-in to run locally."
              : "Sign in with your ClipForge email and password."}
          </p>

          <Suspense fallback={<div className="mt-6 h-48" />}>
            <LoginForm guestEnabled={guestEnabled} />
          </Suspense>

          <p className="mt-6 text-center text-sm text-muted">
            New here?{" "}
            <Link href="/register" className="text-ink underline decoration-acid underline-offset-4">
              Create an account
            </Link>
          </p>
        </div>
      </section>
    </main>
  );
}
