"use client";

import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { useState } from "react";

interface SkipSignInButtonProps {
  callbackUrl?: string;
  label?: string;
  pendingLabel?: string;
  className?: string;
  disabled?: boolean;
  onStart?: () => void;
  onPendingChange?: (pending: boolean) => void;
  onError?: (message: string) => void;
}

export function SkipSignInButton({
  callbackUrl = "/dashboard",
  label = "Skip sign-in (continue locally)",
  pendingLabel = "Entering local workspace...",
  className,
  disabled = false,
  onStart,
  onPendingChange,
  onError,
}: SkipSignInButtonProps) {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  function reportError(message: string) {
    if (onError) {
      onError(message);
    } else {
      setLocalError(message);
    }
  }

  async function handleClick() {
    setLocalError(null);
    onStart?.();
    setSubmitting(true);
    onPendingChange?.(true);
    try {
      const result = await signIn("guest", { redirect: false });
      if (!result || result.error || !result.ok) {
        reportError("Local guest access is unavailable.");
        return;
      }
      router.push(callbackUrl);
      router.refresh();
    } catch {
      reportError("Could not reach the server. Try again.");
    } finally {
      setSubmitting(false);
      onPendingChange?.(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={handleClick}
        disabled={disabled || submitting}
        className={
          className ??
          "inline-flex h-11 w-full items-center justify-center rounded-full bg-bg px-5 text-sm font-medium text-ink outline outline-1 outline-white/15 transition-colors hover:text-acid active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
        }
      >
        {submitting ? pendingLabel : label}
      </button>
      {!onError && localError ? (
        <div
          role="alert"
          className="fixed bottom-5 right-5 z-50 flex max-w-sm items-start gap-3 rounded-xl bg-raise px-4 py-3 text-sm text-[#ffb4ab] shadow-card outline outline-1 outline-[#ff5d4a]/40"
        >
          <p className="flex-1">{localError}</p>
          <button
            type="button"
            onClick={() => setLocalError(null)}
            aria-label="Dismiss error"
            className="text-xs text-muted transition-colors hover:text-ink"
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </>
  );
}
