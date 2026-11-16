const apiUrl =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function isGuestAuthEnabled(): Promise<boolean> {
  try {
    const response = await fetch(`${apiUrl}/api/v1/auth/config`, {
      cache: "no-store",
      signal: AbortSignal.timeout(1500),
    });
    if (!response.ok) {
      return false;
    }
    const data = (await response.json()) as { guest_enabled?: unknown };
    return data.guest_enabled === true;
  } catch {
    return false;
  }
}
