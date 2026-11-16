"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { signIn } from "next-auth/react";
import { FormEvent, useState } from "react";

import { SkipSignInButton } from "../skip-signin-button";

function safeCallbackUrl(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) {
    return "/dashboard";
  }
  return raw;
}

export function LoginForm({ guestEnabled }: { guestEnabled: boolean }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [guestSubmitting, setGuestSubmitting] = useState(false);

  const busy = submitting || guestSubmitting;
  const callbackUrl = safeCallbackUrl(searchParams.get("callbackUrl"));

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      const result = await signIn("credentials", {
        email,
        password,
        redirect: false,
      });

      if (!result || result.error || !result.ok) {
        setError("Invalid email or password.");
        return;
      }

      router.push(callbackUrl);
      router.refresh();
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mt-6">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="email" className="block font-mono text-xs uppercase tracking-wider text-muted">
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid"
            placeholder="streamer@example.com"
          />
        </div>

        <div>
          <label
            htmlFor="password"
            className="block font-mono text-xs uppercase tracking-wider text-muted"
          >
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid"
            placeholder="••••••••••"
          />
        </div>

        {error ? (
          <p
            role="alert"
            className="rounded-xl bg-[#ff5d4a]/15 px-3.5 py-2.5 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
          >
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          disabled={busy}
          className="inline-flex h-11 w-full items-center justify-center rounded-full bg-acid px-5 text-sm font-medium text-[var(--acid-ink)] transition-transform active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? "Signing in..." : "Sign in"}
        </button>
      </form>

      {guestEnabled ? (
        <div className="mt-5 border-t border-white/10 pt-5">
          <SkipSignInButton
            callbackUrl={callbackUrl}
            disabled={submitting}
            onStart={() => setError(null)}
            onPendingChange={setGuestSubmitting}
            onError={(msg) => setError(msg)}
          />
          <p className="mt-2 text-center font-mono text-[11px] text-muted">
            Local-first mode · uses a local workspace without an account
          </p>
        </div>
      ) : null}
    </div>
  );
}
