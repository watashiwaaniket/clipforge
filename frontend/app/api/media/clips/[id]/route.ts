import { NextRequest, NextResponse } from "next/server";

import { auth } from "../../../../../auth";
import { isUuid } from "../../../../../lib/jobs";

export const dynamic = "force-dynamic";

const FORWARDED_HEADERS = [
  "content-type",
  "content-length",
  "content-range",
  "accept-ranges",
  "content-disposition",
  "x-content-type-options",
  "cache-control",
] as const;

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ id: string }> }
) {
  const { id } = await context.params;
  if (!isUuid(id)) {
    return NextResponse.json({ detail: "Not found." }, { status: 404 });
  }

  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Sign in required." }, { status: 401 });
  }

  const apiUrl = (
    process.env.API_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    "http://localhost:8000"
  ).replace(/\/+$/, "");

  const downloadQuery =
    request.nextUrl.searchParams.get("download") === "1" ? "?download=1" : "";
  const upstreamUrl = `${apiUrl}/api/v1/clips/${id}/video${downloadQuery}`;

  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Authorization", `Bearer ${session.accessToken}`);
  const range = request.headers.get("range");
  if (range) {
    upstreamHeaders.set("Range", range);
  }

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, {
      method: "GET",
      headers: upstreamHeaders,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { detail: "Could not reach clip storage." },
      { status: 502 }
    );
  }

  const responseHeaders = new Headers();
  for (const name of FORWARDED_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) {
      responseHeaders.set(name, value);
    }
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
