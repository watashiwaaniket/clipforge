"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { FormEvent, useState } from "react";

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function RegisterPage() {
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
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 pb-16 pt-6 sm:px-8">
      <header className="flex items-center justify-between">
        <Link href="/" className="inline-flex h-10 items-center font-medium tracking-tight">
          ClipForge
        </Link>
        <Link
          href="/login"
          className="inline-flex h-10 items-center rounded-full bg-raise px-4 text-sm text-ink shadow-card transition-colors hover:text-acid"
        >
          Sign in
        </Link>
      </header>

      <section className="mx-auto mt-16 w-full max-w-md rounded-[28px] bg-raise p-2 shadow-card">
        <div className="rounded-[20px] px-5 py-6 sm:px-6">
          <p className="font-mono text-xs uppercase tracking-[0.16em] text-acid">Register</p>
          <h1 className="mt-2 text-2xl font-medium tracking-tight">Create your account</h1>
          <p className="mt-1 text-sm text-muted">
            Bring your own OpenAI-compatible API key after signing in.
          </p>

          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
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

          <p className="mt-6 text-center text-sm text-muted">
            Already have an account?{" "}
            <Link href="/login" className="text-ink underline decoration-acid underline-offset-4">
              Sign in
            </Link>
          </p>
        </div>
      </section>
    </main>
  );
}
