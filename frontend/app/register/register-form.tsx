"use client";

import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { FormEvent, useState } from "react";

import { SkipSignInButton } from "../skip-signin-button";

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function RegisterForm({ guestEnabled }: { guestEnabled: boolean }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (password.length < 10) {
      setError("Password must be at least 10 characters.");
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch(`${apiUrl}/api/v1/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (!response.ok) {
        let message = "Could not create account.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep generic fallback on malformed JSON
        }
        setError(message);
        return;
      }

      const signInResult = await signIn("credentials", {
        email,
        password,
        redirect: false,
      });

      if (!signInResult || signInResult.error || !signInResult.ok) {
        router.push("/login");
        return;
      }

      router.push("/dashboard");
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
          <label
            htmlFor="email"
            className="block font-mono text-xs uppercase tracking-wider text-muted"
          >
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
            autoComplete="new-password"
            required
            minLength={10}
            maxLength={128}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid"
            placeholder="At least 10 characters"
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
          disabled={submitting}
          className="inline-flex h-11 w-full items-center justify-center rounded-full bg-acid px-5 text-sm font-medium text-[var(--acid-ink)] transition-transform active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? "Creating account..." : "Create account"}
        </button>
      </form>

      {guestEnabled ? (
        <div className="mt-5 border-t border-white/10 pt-5">
          <SkipSignInButton onError={(msg) => setError(msg)} />
        </div>
      ) : null}
    </div>
  );
}
