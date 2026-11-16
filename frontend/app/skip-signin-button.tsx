"use client";

import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { useState } from "react";

interface SkipSignInButtonProps {
  callbackUrl?: string;
  label?: string;
  pendingLabel?: string;
  className?: string;
  onError?: (message: string) => void;
}

export function SkipSignInButton({
  callbackUrl = "/dashboard",
  label = "Skip sign-in (continue locally)",
  pendingLabel = "Entering local workspace...",
  className,
  onError,
}: SkipSignInButtonProps) {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);

  async function handleClick() {
    setSubmitting(true);
    try {
      const result = await signIn("guest", { redirect: false });
      if (!result || result.error || !result.ok) {
        onError?.("Local guest access is unavailable.");
        return;
      }
      router.push(callbackUrl);
      router.refresh();
    } catch {
      onError?.("Could not reach the server. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={submitting}
      className={
        className ??
        "inline-flex h-11 w-full items-center justify-center rounded-full bg-bg px-5 text-sm font-medium text-ink outline outline-1 outline-white/15 transition-colors hover:text-acid active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
      }
    >
      {submitting ? pendingLabel : label}
    </button>
  );
}
