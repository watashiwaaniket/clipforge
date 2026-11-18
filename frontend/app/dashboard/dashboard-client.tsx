"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  createJob,
  deleteApiKey,
  fetchJobs,
  fetchKeyMetadata,
  fetchProviders,
  probeAntigravityModels,
  ProviderInfo,
  ProviderModel,
  saveApiKey,
} from "../../lib/api";
import {
  ACTIVE_STATUSES,
  formatRelativeTime,
  listRefetchInterval,
  truncateUrl,
} from "../../lib/jobs";
import { WorkspaceHeader } from "../workspace-header";

const FALLBACK_PROVIDERS: ProviderInfo[] = [
  {
    id: "openai",
    name: "OpenAI",
    requires_api_key: true,
    locked_base_url: true,
    requires_custom_config: false,
    default_base_url: "https://api.openai.com/v1",
    default_model: "gpt-4o-mini",
    connected: false,
    model_source: "curated",
    models: [{ id: "gpt-4o-mini", name: "gpt-4o-mini" }],
  },
  {
    id: "groq",
    name: "Groq",
    requires_api_key: true,
    locked_base_url: true,
    requires_custom_config: false,
    default_base_url: "https://api.groq.com/openai/v1",
    default_model: "llama-3.3-70b-versatile",
    connected: false,
    model_source: "curated",
    models: [{ id: "llama-3.3-70b-versatile", name: "llama-3.3-70b-versatile" }],
  },
  {
    id: "together",
    name: "Together AI",
    requires_api_key: true,
    locked_base_url: true,
    requires_custom_config: false,
    default_base_url: "https://api.together.xyz/v1",
    default_model: "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    connected: false,
    model_source: "curated",
    models: [
      {
        id: "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        name: "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
      },
    ],
  },
  {
    id: "ollama",
    name: "Ollama (local)",
    requires_api_key: true,
    locked_base_url: false,
    requires_custom_config: false,
    default_base_url: "http://host.docker.internal:11434/v1",
    default_model: "llama3.1",
    connected: false,
    model_source: "curated",
    models: [{ id: "llama3.1", name: "llama3.1" }],
  },
  {
    id: "compatible",
    name: "Custom OpenAI-compatible",
    requires_api_key: true,
    locked_base_url: false,
    requires_custom_config: true,
    default_base_url: null,
    default_model: null,
    connected: false,
    model_source: "manual",
    models: [],
  },
  {
    id: "antigravity",
    name: "Antigravity (local CLI / OAuth)",
    requires_api_key: false,
    locked_base_url: true,
    requires_custom_config: false,
    default_base_url: "https://daily-cloudcode-pa.googleapis.com",
    default_model: "gemini-4-hydrogen",
    connected: false,
    model_source: "curated",
    models: [{ id: "gemini-4-hydrogen", name: "gemini-4-hydrogen" }],
  },
];

const CUSTOM_MODEL_SENTINEL = "__custom__";

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

function errorDetail(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return err.detail;
  }
  if (err instanceof Error && err.message) {
    return err.message;
  }
  return fallback;
}

export function DashboardClient() {
  const { data: session, status: sessionStatus } = useSession();
  const accessToken = session?.accessToken;
  const queryClient = useQueryClient();

  const [editingKey, setEditingKey] = useState(false);
  const [providerId, setProviderId] = useState("openai");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [baseUrlInput, setBaseUrlInput] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [probedAntigravity, setProbedAntigravity] = useState<{
    connected: boolean;
    model_source: string;
    default_model: string | null;
    models: ProviderModel[];
  } | null>(null);
  const [keyFormError, setKeyFormError] = useState<string | null>(null);

  const [vodUrl, setVodUrl] = useState("");
  const [jobFormError, setJobFormError] = useState<string | null>(null);

  const keyQuery = useQuery({
    queryKey: ["key"],
    queryFn: () => fetchKeyMetadata(accessToken!),
    enabled: Boolean(accessToken),
  });

  const providersQuery = useQuery({
    queryKey: ["providers"],
    queryFn: () => fetchProviders(accessToken!),
    enabled: Boolean(accessToken),
  });

  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: () => fetchJobs(accessToken!),
    enabled: Boolean(accessToken),
    refetchOnWindowFocus: true,
    refetchInterval: (query) =>
      query.state.data
        ? listRefetchInterval(query.state.data.map((row) => row.status))
        : false,
  });

  const providers = providersQuery.data?.providers ?? FALLBACK_PROVIDERS;
  const selectedProvider = useMemo(
    () => providers.find((item) => item.id === providerId) ?? providers[0],
    [providers, providerId]
  );

  const activeModels = useMemo(() => {
    if (selectedProvider.id === "antigravity" && probedAntigravity) {
      return probedAntigravity.models;
    }
    return selectedProvider.models;
  }, [selectedProvider, probedAntigravity]);

  const effectiveDefaultModel =
    selectedProvider.id === "antigravity" && probedAntigravity
      ? probedAntigravity.default_model
      : selectedProvider.default_model;

  const antigravityConnected =
    selectedProvider.id === "antigravity"
      ? (probedAntigravity?.connected ?? selectedProvider.connected)
      : selectedProvider.connected;

  useEffect(() => {
    if (selectedProvider.requires_custom_config) {
      setSelectedModel(CUSTOM_MODEL_SENTINEL);
      return;
    }
    const candidate = effectiveDefaultModel ?? activeModels[0]?.id ?? "";
    if (!selectedModel || selectedModel === CUSTOM_MODEL_SENTINEL) {
      setSelectedModel(candidate);
    } else if (
      activeModels.length > 0 &&
      !activeModels.some((m) => m.id === selectedModel)
    ) {
      setSelectedModel(candidate);
    }
  }, [
    selectedProvider.id,
    selectedProvider.requires_custom_config,
    effectiveDefaultModel,
    activeModels,
    selectedModel,
  ]);

  function handleProviderChange(nextId: string) {
    const next = providers.find((item) => item.id === nextId);
    setProviderId(nextId);
    setApiKeyInput("");
    setBaseUrlInput("");
    setCustomModel("");
    setKeyFormError(null);
    if (nextId !== "antigravity") {
      setProbedAntigravity(null);
    }
    if (next?.requires_custom_config) {
      setSelectedModel(CUSTOM_MODEL_SENTINEL);
    } else {
      setSelectedModel(next?.default_model ?? next?.models[0]?.id ?? "");
    }
  }

  function startReplace() {
    const current = keyQuery.data;
    if (current?.configured && current.provider) {
      const target = providers.find((p) => p.id === current.provider);
      setProviderId(current.provider);
      setBaseUrlInput(target && !target.locked_base_url ? (current.base_url ?? "") : "");
      if (target?.requires_custom_config) {
        setSelectedModel(CUSTOM_MODEL_SENTINEL);
        setCustomModel(current.model ?? "");
      } else if (
        current.model &&
        target?.models.some((m) => m.id === current.model)
      ) {
        setSelectedModel(current.model);
        setCustomModel("");
      } else if (current.model) {
        setSelectedModel(CUSTOM_MODEL_SENTINEL);
        setCustomModel(current.model);
      } else {
        setSelectedModel(target?.default_model ?? "");
        setCustomModel("");
      }
    }
    setApiKeyInput("");
    setKeyFormError(null);
    setEditingKey(true);
  }

  const probeModelsMutation = useMutation({
    mutationFn: () => probeAntigravityModels(accessToken!, apiKeyInput || null),
    onSuccess: (data) => {
      setKeyFormError(null);
      setProbedAntigravity({
        connected: data.connected,
        model_source: data.model_source,
        default_model: data.default_model,
        models: data.models,
      });
      if (data.default_model) {
        setSelectedModel(data.default_model);
      } else if (data.models[0]) {
        setSelectedModel(data.models[0].id);
      }
    },
    onError: (err) => {
      setKeyFormError(errorDetail(err, "Could not probe Antigravity models."));
    },
  });

  const saveKeyMutation = useMutation({
    mutationFn: () => {
      const chosenModel =
        selectedProvider.requires_custom_config || selectedModel === CUSTOM_MODEL_SENTINEL
          ? customModel.trim() || null
          : selectedModel.trim() || null;

      return saveApiKey(accessToken!, {
        provider: selectedProvider.id,
        api_key: apiKeyInput.trim() ? apiKeyInput.trim() : null,
        base_url:
          !selectedProvider.locked_base_url && baseUrlInput.trim()
            ? baseUrlInput.trim()
            : null,
        model: chosenModel,
      });
    },
    onSuccess: async () => {
      setApiKeyInput("");
      setKeyFormError(null);
      setEditingKey(false);
      await queryClient.invalidateQueries({ queryKey: ["key"] });
    },
    onError: (err) => {
      setKeyFormError(errorDetail(err, "Could not save API key."));
    },
  });

  const deleteKeyMutation = useMutation({
    mutationFn: () => deleteApiKey(accessToken!),
    onSuccess: async () => {
      setApiKeyInput("");
      setKeyFormError(null);
      setEditingKey(false);
      await queryClient.invalidateQueries({ queryKey: ["key"] });
    },
    onError: (err) => {
      setKeyFormError(errorDetail(err, "Could not delete API key."));
    },
  });

  const createJobMutation = useMutation({
    mutationFn: (url: string) => createJob(accessToken!, url),
    onSuccess: async () => {
      setVodUrl("");
      setJobFormError(null);
      await queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (err) => {
      setJobFormError(errorDetail(err, "Could not start job."));
    },
  });

  function handleSaveKeySubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setKeyFormError(null);
    saveKeyMutation.mutate();
  }

  function handleGenerateSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setJobFormError(null);
    const trimmed = vodUrl.trim();
    if (!trimmed) {
      setJobFormError("Paste a public Twitch VOD or YouTube video URL.");
      return;
    }
    createJobMutation.mutate(trimmed);
  }

  const keyConfigured = Boolean(keyQuery.data?.configured);
  const showKeyForm = !keyConfigured || editingKey;

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 pb-16 pt-6 sm:px-8">
      <WorkspaceHeader />

      {sessionStatus === "loading" || (accessToken && keyQuery.isLoading) ? (
        <section className="mt-10 rounded-[28px] bg-raise p-8 shadow-card">
          <p className="font-mono text-xs text-muted">Loading workspace...</p>
        </section>
      ) : (
        <div className="mt-10 space-y-8">
          <section className="rounded-[28px] bg-raise p-2 shadow-card">
            <div className="rounded-[20px] px-4 py-6 sm:px-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="font-mono text-xs uppercase tracking-[0.16em] text-acid">
                    Step 01 · LLM provider
                  </p>
                  <h1 className="mt-2 text-2xl font-medium tracking-tight">
                    {keyConfigured && !editingKey
                      ? "Provider configured"
                      : "Configure your LLM key"}
                  </h1>
                  <p className="mt-1 text-sm text-muted">
                    Encrypted at rest with Fernet and decrypted only inside the worker during
                    scoring.
                  </p>
                </div>

                {keyConfigured && !editingKey ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={startReplace}
                      className="inline-flex h-10 items-center rounded-full bg-bg px-4 text-sm text-ink outline outline-1 outline-white/10 transition-colors hover:text-acid"
                    >
                      Replace
                    </button>
                    <button
                      type="button"
                      disabled={deleteKeyMutation.isPending}
                      onClick={() => deleteKeyMutation.mutate()}
                      className="inline-flex h-10 items-center rounded-full bg-[#ff5d4a]/15 px-4 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30 transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {deleteKeyMutation.isPending ? "Deleting..." : "Delete"}
                    </button>
                  </div>
                ) : null}
              </div>

              {keyConfigured && !editingKey && keyQuery.data ? (
                <dl className="mt-5 grid gap-4 rounded-2xl bg-bg p-4 outline outline-1 outline-white/10 sm:grid-cols-3">
                  <div>
                    <dt className="font-mono text-[11px] uppercase tracking-wider text-muted">
                      Provider
                    </dt>
                    <dd className="mt-1 font-mono text-sm text-ink">
                      {keyQuery.data.provider}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-mono text-[11px] uppercase tracking-wider text-muted">
                      Model
                    </dt>
                    <dd className="mt-1 break-all font-mono text-sm text-ink">
                      {keyQuery.data.model}
                    </dd>
                  </div>
                  {keyQuery.data.base_url ? (
                    <div>
                      <dt className="font-mono text-[11px] uppercase tracking-wider text-muted">
                        Base URL
                      </dt>
                      <dd className="mt-1 break-all font-mono text-sm text-muted">
                        {keyQuery.data.base_url}
                      </dd>
                    </div>
                  ) : null}
                </dl>
              ) : null}

              {showKeyForm ? (
                <form onSubmit={handleSaveKeySubmit} className="mt-6 space-y-4">
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <label
                        htmlFor="provider"
                        className="block font-mono text-xs uppercase tracking-wider text-muted"
                      >
                        Provider
                      </label>
                      <select
                        id="provider"
                        value={selectedProvider.id}
                        onChange={(e) => handleProviderChange(e.target.value)}
                        className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 focus:outline-2 focus:outline-acid"
                      >
                        {providers.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                      {selectedProvider.id === "antigravity" ||
                      selectedProvider.id === "ollama" ? (
                        <p className="mt-2 font-mono text-[11px] text-muted">
                          {antigravityConnected
                            ? "Live session detected"
                            : selectedProvider.id === "antigravity"
                              ? "No local CLI/bridge detected yet — start local bridge or paste an OAuth token."
                              : "Local Ollama not reachable yet — default http://host.docker.internal:11434/v1"}
                        </p>
                      ) : null}
                    </div>

                    <div>
                      <div className="flex items-center justify-between gap-2">
                        <label
                          htmlFor="api-key"
                          className="block font-mono text-xs uppercase tracking-wider text-muted"
                        >
                          {selectedProvider.requires_api_key
                            ? "API Key"
                            : "OAuth Token / JSON (optional)"}
                        </label>
                        {selectedProvider.id === "antigravity" ? (
                          <button
                            type="button"
                            disabled={probeModelsMutation.isPending}
                            onClick={() => probeModelsMutation.mutate()}
                            className="font-mono text-[11px] text-acid hover:underline disabled:opacity-60"
                          >
                            {probeModelsMutation.isPending ? "Checking..." : "Check models"}
                          </button>
                        ) : null}
                      </div>
                      <input
                        id="api-key"
                        name="api_key"
                        type="password"
                        autoComplete="off"
                        required={selectedProvider.requires_api_key}
                        value={apiKeyInput}
                        onChange={(e) => setApiKeyInput(e.target.value)}
                        placeholder={
                          selectedProvider.requires_api_key
                            ? "sk-..."
                            : "Leave blank to use local agy CLI / bridge / OAuth"
                        }
                        className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid"
                      />
                    </div>
                  </div>

                  <div className="grid gap-4 sm:grid-cols-2">
                    {!selectedProvider.locked_base_url ? (
                      <div>
                        <label
                          htmlFor="base-url"
                          className="block font-mono text-xs uppercase tracking-wider text-muted"
                        >
                          Base URL{" "}
                          {selectedProvider.requires_custom_config ? "(required)" : "(optional)"}
                        </label>
                        <input
                          id="base-url"
                          name="base_url"
                          type="url"
                          required={selectedProvider.requires_custom_config}
                          value={baseUrlInput}
                          onChange={(e) => setBaseUrlInput(e.target.value)}
                          placeholder={
                            selectedProvider.default_base_url ?? "https://api.example.com/v1"
                          }
                          className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid"
                        />
                      </div>
                    ) : null}

                    <div>
                      <label
                        htmlFor="model-select"
                        className="block font-mono text-xs uppercase tracking-wider text-muted"
                      >
                        Model{" "}
                        {selectedProvider.requires_custom_config ? "(required)" : "(optional)"}
                      </label>
                      {!selectedProvider.requires_custom_config && activeModels.length > 0 ? (
                        <select
                          id="model-select"
                          value={selectedModel}
                          onChange={(e) => setSelectedModel(e.target.value)}
                          className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 focus:outline-2 focus:outline-acid"
                        >
                          {activeModels.map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.name}
                            </option>
                          ))}
                          <option value={CUSTOM_MODEL_SENTINEL}>Custom model...</option>
                        </select>
                      ) : null}

                      {selectedProvider.requires_custom_config ||
                      activeModels.length === 0 ||
                      selectedModel === CUSTOM_MODEL_SENTINEL ? (
                        <input
                          id="custom-model"
                          name="model"
                          type="text"
                          required={selectedProvider.requires_custom_config}
                          value={customModel}
                          onChange={(e) => setCustomModel(e.target.value)}
                          placeholder={effectiveDefaultModel ?? "gpt-4o-mini"}
                          className="mt-2 h-11 w-full rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid"
                        />
                      ) : null}
                    </div>
                  </div>

                  {keyFormError ? (
                    <p
                      role="alert"
                      className="rounded-xl bg-[#ff5d4a]/15 px-3.5 py-2.5 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
                    >
                      {keyFormError}
                    </p>
                  ) : null}

                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      type="submit"
                      disabled={saveKeyMutation.isPending}
                      className="inline-flex h-11 items-center justify-center rounded-full bg-acid px-6 text-sm font-medium text-[var(--acid-ink)] transition-transform active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {saveKeyMutation.isPending ? "Validating..." : "Save key"}
                    </button>
                    {editingKey ? (
                      <button
                        type="button"
                        onClick={() => {
                          setApiKeyInput("");
                          setKeyFormError(null);
                          setEditingKey(false);
                        }}
                        className="inline-flex h-11 items-center rounded-full bg-bg px-5 text-sm text-muted outline outline-1 outline-white/10 transition-colors hover:text-ink"
                      >
                        Cancel
                      </button>
                    ) : null}
                  </div>
                </form>
              ) : null}
            </div>
          </section>

          <section className="rounded-[28px] bg-raise p-2 shadow-card">
            <div className="rounded-[20px] px-4 py-6 sm:px-6">
              <p className="font-mono text-xs uppercase tracking-[0.16em] text-acid">
                Step 02 · New job
              </p>
              <h2 className="mt-2 text-2xl font-medium tracking-tight">Generate clips</h2>
              <p className="mt-1 text-sm text-muted">
                Max 240 minutes, up to 8 clips per job. Twitch VODs expire after 14 days (60 for
                Partners).
              </p>

              {!keyConfigured ? (
                <p className="mt-4 rounded-xl bg-bg px-3.5 py-2.5 font-mono text-xs text-muted outline outline-1 outline-white/10">
                  Save an LLM provider key above before submitting a VOD URL.
                </p>
              ) : null}

              <form onSubmit={handleGenerateSubmit} className="mt-5 flex flex-col gap-3 sm:flex-row">
                <label htmlFor="vod-url" className="sr-only">
                  VOD URL
                </label>
                <input
                  id="vod-url"
                  name="url"
                  type="url"
                  required
                  disabled={!keyConfigured || createJobMutation.isPending}
                  value={vodUrl}
                  onChange={(e) => setVodUrl(e.target.value)}
                  placeholder="https://www.twitch.tv/videos/... or https://www.youtube.com/watch?v=..."
                  className="h-11 flex-1 rounded-xl bg-bg px-3.5 text-sm text-ink outline outline-1 outline-white/10 placeholder:text-muted focus:outline-2 focus:outline-acid disabled:cursor-not-allowed disabled:opacity-50"
                />
                <button
                  type="submit"
                  disabled={!keyConfigured || createJobMutation.isPending}
                  className="inline-flex h-11 items-center justify-center rounded-full bg-acid px-6 text-sm font-medium text-[var(--acid-ink)] transition-transform active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {createJobMutation.isPending ? "Queuing..." : "Generate"}
                </button>
              </form>

              {jobFormError ? (
                <p
                  role="alert"
                  className="mt-4 rounded-xl bg-[#ff5d4a]/15 px-3.5 py-2.5 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
                >
                  {jobFormError}
                </p>
              ) : null}
            </div>
          </section>

          <section className="rounded-[28px] bg-raise p-2 shadow-card">
            <div className="rounded-[20px] px-4 py-6 sm:px-6">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="font-mono text-xs uppercase tracking-[0.16em] text-acid">
                    Recent jobs
                  </p>
                  <h2 className="mt-1 text-xl font-medium tracking-tight">Pipeline queue</h2>
                </div>
                {jobsQuery.data &&
                listRefetchInterval(jobsQuery.data.map((j) => j.status)) !== false ? (
                  <span className="inline-flex items-center gap-2 font-mono text-xs text-muted">
                    <span className="size-2 rounded-full bg-acid" aria-hidden />
                    Polling every 2.5s
                  </span>
                ) : null}
              </div>

              {jobsQuery.isLoading ? (
                <p className="mt-5 font-mono text-xs text-muted">Loading jobs...</p>
              ) : jobsQuery.isError ? (
                <p
                  role="alert"
                  className="mt-5 rounded-xl bg-[#ff5d4a]/15 px-3.5 py-2.5 text-sm text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
                >
                  {errorDetail(jobsQuery.error, "Could not load jobs.")}
                </p>
              ) : !jobsQuery.data || jobsQuery.data.length === 0 ? (
                <p className="mt-5 rounded-2xl bg-bg px-4 py-6 text-sm text-muted outline outline-1 outline-white/10">
                  No jobs yet. Paste a public Twitch VOD or YouTube video URL above to start your
                  first run.
                </p>
              ) : (
                <ul className="mt-5 space-y-3">
                  {jobsQuery.data.map((job) => (
                    <li key={job.id}>
                      <Link
                        href={`/jobs/${job.id}`}
                        className="block rounded-2xl bg-bg p-4 outline outline-1 outline-white/10 transition-colors hover:outline-acid/50"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex flex-wrap items-center gap-2.5">
                            <span
                              className={`inline-flex h-6 items-center rounded-full px-2.5 font-mono text-xs ${statusBadgeClass(
                                job.status
                              )}`}
                            >
                              {job.status}
                            </span>
                            <span className="font-mono text-sm text-ink">
                              {truncateUrl(job.source_url)}
                            </span>
                          </div>
                          <span className="font-mono text-xs tabular-nums text-muted">
                            {formatRelativeTime(job.created_at)}
                          </span>
                        </div>
                        <p className="mt-2 text-sm text-muted">{job.progress_message}</p>
                        {job.error ? (
                          <p
                            role="alert"
                            className="mt-2 rounded-xl bg-[#ff5d4a]/15 px-3 py-2 text-xs text-[#ffb4ab] outline outline-1 outline-[#ff5d4a]/30"
                          >
                            {job.error}
                          </p>
                        ) : null}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
