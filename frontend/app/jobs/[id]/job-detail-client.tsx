"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { useEffect, useState } from "react";

import { ApiError, ClipItem, fetchJobDetail } from "../../../lib/api";
import {
  ACTIVE_STATUSES,
  formatClipRange,
  formatDuration,
  formatRelativeTime,
  listRefetchInterval,
  safeHttpUrl,
  truncateUrl,
} from "../../../lib/jobs";
import { WorkspaceHeader } from "../../workspace-header";

function statusBadgeClass(status: string): string {
  if (status === "completed") {
    return "bg-acid/15 text-acid outline outline-1 outline-acid/30";
  }
  if (status === "failed") {
    return "bg-[#ff5d4a]/15 text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30";
  }
  if (ACTIVE_STATUSES.has(status)) {
    return "bg-white/10 text-ink outline outline-1 outline-white/15";
  }
  return "bg-bg text-muted outline outline-1 outline-white/10";
}

function ClipCard({ clip }: { clip: ClipItem }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function handleCopyTitle() {
    try {
      await navigator.clipboard.writeText(clip.title);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <article className="flex flex-col justify-between rounded-[24px] bg-raise p-2 shadow-card outline outline-1 outline-white/10">
      <div className="flex flex-col gap-4 rounded-[18px] p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex h-7 items-center rounded-full bg-acid px-2.5 font-mono text-xs font-medium tabular-nums text-[var(--acid-ink)]">
              {clip.score}
            </span>
            <span className="font-mono text-xs tabular-nums text-muted">
              {formatClipRange(clip.start_s, clip.end_s)}
            </span>
          </div>
          <span className="rounded-full bg-bg px-2.5 py-1 font-mono text-[11px] text-muted outline outline-1 outline-white/10">
            {clip.score_source}
          </span>
        </div>

        {clip.status === "ready" ? (
          <div className="overflow-hidden rounded-2xl bg-black outline outline-1 outline-white/10">
            <video
              controls
              playsInline
              preload="metadata"
              src={`/api/media/clips/${clip.id}`}
              className="aspect-[9/16] w-full bg-black object-contain"
            />
          </div>
        ) : (
          <div className="flex aspect-[9/16] items-center justify-center rounded-2xl bg-bg p-5 text-center outline outline-1 outline-[#ff5d4a]/30">
            <p className="text-sm text-[#ffb4ab]">
              {clip.error ?? "This clip could not be exported."}
            </p>
          </div>
        )}

        <div>
          <div className="flex items-start justify-between gap-2">
            <h2 className="text-base font-medium leading-snug text-ink">{clip.title}</h2>
            <button
              type="button"
              onClick={handleCopyTitle}
              className="shrink-0 rounded-full bg-bg px-3 py-1 font-mono text-[11px] text-muted outline outline-1 outline-white/10 transition-colors hover:text-acid"
            >
              {copied ? "Copied" : "Copy title"}
            </button>
          </div>
          <p className="mt-2 text-pretty text-sm leading-relaxed text-muted">{clip.hook}</p>
        </div>
      </div>

      {clip.status === "ready" ? (
        <div className="px-3 pb-3 pt-1">
          <a
            href={`/api/media/clips/${clip.id}?download=1`}
            download
            className="inline-flex h-10 w-full items-center justify-center rounded-full bg-acid px-4 text-sm font-medium text-[var(--acid-ink)] transition-transform active:scale-[0.98]"
          >
            Download MP4
          </a>
        </div>
      ) : null}
    </article>
  );
}

export function JobDetailClient({ jobId }: { jobId: string }) {
  const { data: session, status: sessionStatus } = useSession();
  const accessToken = session?.accessToken;

  const jobQuery = useQuery({
    queryKey: ["jobs", jobId],
    queryFn: () => fetchJobDetail(accessToken!, jobId),
    enabled: Boolean(accessToken),
    refetchOnWindowFocus: true,
    refetchInterval: (query) =>
      query.state.data ? listRefetchInterval([query.state.data.status]) : false,
  });

  const job = jobQuery.data;
  const safeThumb = job?.thumbnail_url ? safeHttpUrl(job.thumbnail_url) : null;
  const safeSource = job ? safeHttpUrl(job.source_url) : null;
  const formattedDuration = job ? formatDuration(job.duration_s) : null;

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 pb-16 pt-6 sm:px-8">
      <WorkspaceHeader showDashboardLink />

      {sessionStatus === "loading" || (accessToken && jobQuery.isLoading) ? (
        <section className="mt-10 rounded-[28px] bg-raise p-8 shadow-card">
          <p className="font-mono text-xs text-muted">Loading job...</p>
        </section>
      ) : jobQuery.isError ? (
        <section className="mt-10 rounded-[28px] bg-raise p-6 shadow-card">
          <p
            role="alert"
            className="rounded-xl bg-[#ff5d4a]/15 px-3.5 py-2.5 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
          >
            {jobQuery.error instanceof ApiError
              ? jobQuery.error.detail
              : "Could not load job."}
          </p>
          <Link
            href="/dashboard"
            className="mt-4 inline-flex h-10 items-center rounded-full bg-bg px-4 text-sm text-ink outline outline-1 outline-white/10 hover:text-acid"
          >
            Back to dashboard
          </Link>
        </section>
      ) : job ? (
        <div className="mt-10 space-y-8">
          <section className="rounded-[28px] bg-raise p-2 shadow-card">
            <div className="rounded-[20px] px-4 py-6 sm:px-6">
              <div className="flex flex-wrap items-center gap-2 font-mono text-xs text-muted">
                <span className="uppercase tracking-wider text-acid">{job.platform}</span>
                <span>·</span>
                <span className="tabular-nums">{formatRelativeTime(job.created_at)}</span>
                {formattedDuration ? (
                  <>
                    <span>·</span>
                    <span className="tabular-nums">{formattedDuration}</span>
                  </>
                ) : null}
              </div>

              <h1 className="mt-2 text-balance text-2xl font-medium tracking-tight sm:text-3xl">
                {job.title ?? truncateUrl(job.source_url, 80)}
              </h1>

              {safeSource ? (
                <a
                  href={safeSource}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 inline-block break-all font-mono text-xs text-muted underline decoration-white/20 underline-offset-4 hover:text-ink"
                >
                  {job.source_url}
                </a>
              ) : null}

              {safeThumb ? (
                <div className="mt-5">
                  <img
                    src={safeThumb}
                    alt=""
                    className="aspect-video w-full max-w-md rounded-2xl object-cover outline outline-1 outline-white/10"
                  />
                </div>
              ) : null}

              <div className="mt-5 flex flex-wrap items-center gap-3">
                <span
                  className={`inline-flex h-7 items-center rounded-full px-3 font-mono text-xs ${statusBadgeClass(
                    job.status
                  )}`}
                >
                  {job.status}
                </span>
                <p className="text-sm text-ink">{job.progress_message}</p>
              </div>

              {job.error ? (
                <div
                  role="alert"
                  className="mt-4 rounded-xl bg-[#ff5d4a]/15 px-3.5 py-2.5 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
                >
                  {job.error}
                </div>
              ) : null}
            </div>
          </section>

          {job.clips.length > 0 ? (
            <section>
              <div className="mb-4 flex items-center justify-between">
                <h2 className="font-mono text-xs uppercase tracking-[0.16em] text-acid">
                  Clips ({job.clips.length})
                </h2>
              </div>
              <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                {job.clips.map((clip) => (
                  <ClipCard key={clip.id} clip={clip} />
                ))}
              </div>
            </section>
          ) : ACTIVE_STATUSES.has(job.status) ? (
            <section className="rounded-[24px] bg-raise px-6 py-8 shadow-card">
              <p className="font-mono text-xs text-muted">
                Clips will appear here once scoring and rendering finish. Polling every 2.5s.
              </p>
            </section>
          ) : null}
        </div>
      ) : null}
    </main>
  );
}
