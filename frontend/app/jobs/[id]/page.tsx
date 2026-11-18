import { redirect } from "next/navigation";

import { auth } from "../../../auth";
import { JobDetailClient } from "./job-detail-client";

export const dynamic = "force-dynamic";

export default async function JobDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const session = await auth();
  if (!session) {
    redirect(`/login?callbackUrl=${encodeURIComponent(`/jobs/${id}`)}`);
  }

  return <JobDetailClient jobId={id} />;
}
