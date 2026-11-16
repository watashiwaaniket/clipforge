import Link from "next/link";

import { auth } from "../auth";
import { isGuestAuthEnabled } from "../lib/auth-mode";
import { HealthPill } from "./health-pill";
import { SkipSignInButton } from "./skip-signin-button";

export const dynamic = "force-dynamic";

const steps = [
  {
    n: "01",
    title: "Paste a VOD",
    body: "A public Twitch VOD or YouTube video. Subscriber-only links stop with a clear error.",
  },
  {
    n: "02",
    title: "Bring a key",
    body: "Any OpenAI-compatible endpoint. Encrypted at rest. Decrypted in the worker, for that job only.",
  },
  {
    n: "03",
    title: "Generate",
    body: "Download, word-level transcript, viral-moment score, 9:16 crop, captions burned in.",
  },
];

const states = [
  "queued",
  "downloading",
  "transcribing",
  "scoring",
  "clipping",
  "completed",
];

const specs = [
  { value: "240", unit: "min", label: "Max VOD" },
  { value: "8", unit: "", label: "Clips per job" },
  { value: "1080×1920", unit: "", label: "Center crop" },
];

export default async function HomePage() {
  const [session, guestEnabled] = await Promise.all([auth(), isGuestAuthEnabled()]);

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 pb-16 pt-6 sm:px-8">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <a href="#top" className="inline-flex h-10 items-center font-medium tracking-tight">
          ClipForge
        </a>
        <div className="flex flex-wrap items-center gap-3">
          <HealthPill />
          {!session && guestEnabled ? (
            <SkipSignInButton
              label="Skip sign-in"
              pendingLabel="Opening..."
              className="inline-flex h-10 items-center rounded-full bg-raise px-4 text-sm text-ink shadow-card transition-colors hover:text-acid disabled:cursor-not-allowed disabled:opacity-60"
            />
          ) : null}
          <Link
            href={session ? "/dashboard" : "/login"}
            className="inline-flex h-10 items-center rounded-full bg-raise px-4 text-sm text-ink shadow-card transition-colors hover:text-acid"
          >
            {session ? "Dashboard" : "Sign in"}
          </Link>
          <a
            href="#pipeline"
            className="inline-flex h-10 items-center rounded-full bg-acid px-4 text-sm font-medium text-[var(--acid-ink)] transition-transform active:scale-[0.96]"
          >
            How a job runs
          </a>
        </div>
      </header>

      <section id="top" className="grid items-center gap-14 py-16 lg:grid-cols-[minmax(0,1fr)_320px] lg:py-24">
        <div>
          <p className="animate-rise font-mono text-xs uppercase tracking-[0.16em] text-acid">
            Twitch · YouTube · Bring your own key
          </p>
          <h1
            className="animate-rise mt-4 max-w-xl text-balance text-5xl font-medium leading-[1.05] tracking-tight sm:text-6xl"
            style={{ animationDelay: "80ms" }}
          >
            Viral vertical clips from the VOD you already have.
          </h1>
          <p
            className="animate-rise mt-5 max-w-lg text-pretty text-lg leading-relaxed text-muted"
            style={{ animationDelay: "160ms" }}
          >
            Built for GTA 6 streamers. Paste a link, add your LLM key, and get scored 9:16s
            ready for TikTok and Shorts. Your key. Your machine.
          </p>

          <ol className="mt-10 grid gap-3 sm:grid-cols-3">
            {steps.map((step, index) => (
              <li
                key={step.n}
                className="animate-rise rounded-[20px] bg-raise p-2 shadow-card"
                style={{ animationDelay: `${240 + index * 90}ms` }}
              >
                <div className="rounded-xl px-3 py-3">
                  <p className="font-mono text-xs tabular-nums text-acid">{step.n}</p>
                  <h2 className="mt-3 text-base font-medium">{step.title}</h2>
                  <p className="mt-1 text-pretty text-sm leading-relaxed text-muted">{step.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>

        <figure
          className="animate-rise mx-auto w-[280px] rounded-[36px] bg-raise p-2 shadow-card outline outline-1 outline-white/10"
          style={{ animationDelay: "200ms" }}
        >
          <div className="relative aspect-[9/16] overflow-hidden rounded-[28px] bg-[#12140e]">
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(223,242,90,0.35),transparent_42%),linear-gradient(160deg,#2a2418_0%,#12140e_48%,#1b2410_100%)]" />
            <figcaption className="absolute left-3 top-3 rounded-full bg-black/40 px-2.5 py-1 font-mono text-[11px] text-ink backdrop-blur-sm">
              Sample clip
            </figcaption>
            <p className="absolute right-3 top-3 inline-flex h-7 items-center rounded-full bg-acid px-2 font-mono text-xs font-medium tabular-nums text-[var(--acid-ink)]">
              91
            </p>
            <div className="absolute inset-x-4 bottom-24 text-center">
              <p className="text-[28px] font-medium leading-none tracking-tight text-white [text-shadow:0_2px_0_#000]">
                he just <span className="text-acid">YEETED</span>
              </p>
              <p className="mt-1 text-[28px] font-medium leading-none tracking-tight text-white [text-shadow:0_2px_0_#000]">
                the car
              </p>
            </div>
            <div className="absolute inset-x-3 bottom-3 rounded-2xl bg-black/55 p-3 outline outline-1 outline-white/10 backdrop-blur-sm">
              <p className="text-sm font-medium leading-snug">Bridge yeet, no recovery</p>
              <p className="mt-1 text-pretty text-xs leading-relaxed text-muted">
                A clean fail with a shouted reaction. Hook is the launch, payoff is the splash.
              </p>
            </div>
          </div>
        </figure>
      </section>

      <section id="pipeline" className="rounded-[28px] bg-raise p-2 shadow-card">
        <div className="rounded-[20px] px-4 py-6 sm:px-6">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <h2 className="text-balance text-2xl font-medium tracking-tight">One job, forward only</h2>
              <p className="mt-2 max-w-xl text-pretty text-sm leading-relaxed text-muted">
                The API hands back a job id. A worker walks the VOD. Any step can fail the job
                with a message you can read. One bad clip does not take the rest down.
              </p>
            </div>
            <dl className="flex flex-wrap gap-x-6 gap-y-3">
              {specs.map((spec) => (
                <div key={spec.label}>
                  <dt className="font-mono text-[11px] uppercase tracking-wider text-muted">{spec.label}</dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {spec.value}
                    {spec.unit ? <span className="ml-1 text-sm text-muted">{spec.unit}</span> : null}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
          <ol className="mt-6 flex flex-wrap gap-2">
            {states.map((state, index) => (
              <li key={state} className="flex items-center gap-2">
                <span className="inline-flex h-10 items-center rounded-full bg-bg px-3 font-mono text-xs text-ink">
                  {state}
                </span>
                {index < states.length - 1 ? (
                  <span className="text-muted" aria-hidden>
                    →
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        </div>
      </section>

      <footer className="mt-10 flex flex-wrap items-center justify-between gap-3 font-mono text-xs text-muted">
        <Link href="/dashboard" className="transition-colors hover:text-ink">
          Dashboard
        </Link>
        <p className="tabular-nums">localhost:3000 · API :8000</p>
      </footer>
    </main>
  );
}
