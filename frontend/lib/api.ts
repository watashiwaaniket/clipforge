const DEFAULT_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function getApiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_API_URL).replace(/\/+$/, "");
}

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ProviderModel {
  id: string;
  name: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  requires_api_key: boolean;
  locked_base_url: boolean;
  requires_custom_config: boolean;
  default_base_url: string | null;
  default_model: string | null;
  connected: boolean;
  model_source: string;
  models: ProviderModel[];
}

export interface ProviderCatalogResponse {
  providers: ProviderInfo[];
}

export interface ProviderModelsResponse {
  provider: string;
  connected: boolean;
  model_source: string;
  default_model: string | null;
  models: ProviderModel[];
}

export interface ApiKeyMetadata {
  configured: boolean;
  provider?: string;
  model?: string;
  base_url?: string;
}

export interface ApiKeyUpsertPayload {
  provider: string;
  api_key?: string | null;
  base_url?: string | null;
  model?: string | null;
}

export interface ApiKeyUpsertResponse {
  configured: boolean;
  provider: string;
  model: string;
}

export interface JobListItem {
  id: string;
  source_url: string;
  status: string;
  progress_message: string;
  error: string | null;
  created_at: string;
}

export interface ClipItem {
  id: string;
  start_s: number;
  end_s: number;
  score: number;
  title: string;
  hook: string;
  score_source: string;
  caption_status: string;
  status: string;
  error: string | null;
  created_at: string;
}

export interface JobDetail {
  id: string;
  source_url: string;
  platform: string;
  title: string | null;
  duration_s: number | null;
  thumbnail_url: string | null;
  status: string;
  progress_message: string;
  error: string | null;
  created_at: string;
  updated_at: string;
  clips: ClipItem[];
}

export interface JobCreateResponse {
  job_id: string;
}

async function apiFetch<T>(
  path: string,
  accessToken: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${accessToken}`);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });

  if (response.status === 204) {
    return undefined as T;
  }

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const detail =
      payload &&
      typeof payload === "object" &&
      "detail" in payload &&
      typeof (payload as { detail: unknown }).detail === "string"
        ? (payload as { detail: string }).detail
        : `Request failed (${response.status}).`;
    throw new ApiError(response.status, detail);
  }

  return payload as T;
}

export function fetchKeyMetadata(accessToken: string): Promise<ApiKeyMetadata> {
  return apiFetch<ApiKeyMetadata>("/api/v1/keys", accessToken);
}

export function fetchProviders(accessToken: string): Promise<ProviderCatalogResponse> {
  return apiFetch<ProviderCatalogResponse>("/api/v1/providers", accessToken);
}

export function fetchProviderModels(
  accessToken: string,
  provider: string
): Promise<ProviderModelsResponse> {
  return apiFetch<ProviderModelsResponse>(
    `/api/v1/providers/${encodeURIComponent(provider)}/models`,
    accessToken
  );
}

export function probeAntigravityModels(
  accessToken: string,
  apiKey?: string | null
): Promise<ProviderModelsResponse> {
  return apiFetch<ProviderModelsResponse>("/api/v1/providers/antigravity/models", accessToken, {
    method: "POST",
    body: JSON.stringify({ api_key: apiKey?.trim() ? apiKey.trim() : null }),
  });
}

export function saveApiKey(
  accessToken: string,
  payload: ApiKeyUpsertPayload
): Promise<ApiKeyUpsertResponse> {
  return apiFetch<ApiKeyUpsertResponse>("/api/v1/keys", accessToken, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteApiKey(accessToken: string): Promise<void> {
  return apiFetch<void>("/api/v1/keys", accessToken, { method: "DELETE" });
}

export function fetchJobs(accessToken: string): Promise<JobListItem[]> {
  return apiFetch<JobListItem[]>("/api/v1/jobs", accessToken);
}

export function createJob(accessToken: string, url: string): Promise<JobCreateResponse> {
  return apiFetch<JobCreateResponse>("/api/v1/jobs", accessToken, {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export function fetchJobDetail(accessToken: string, jobId: string): Promise<JobDetail> {
  return apiFetch<JobDetail>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, accessToken);
}
