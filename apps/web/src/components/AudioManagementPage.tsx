import {
  ApiError,
  type Locale,
  type PodcastAudioVariantResponse,
  type PodcastEpisodeAdmin,
  type PodcastMetadata,
  type PodcastUploadReason,
} from "@daily-insights/api-client"
import { useForm } from "@tanstack/react-form"
import { useRouter } from "@tanstack/react-router"
import { motion } from "motion/react"
import { useEffect, useRef, useState, type DragEvent } from "react"
import { useTranslation } from "react-i18next"
import { browserPodcastAdminClient } from "#/lib/admin-podcasts"
import { requireCsrfToken } from "#/lib/auth"
import {
  formatChapterText,
  maxPodcastChapters,
  parseChapterText,
} from "#/lib/podcast-chapter-text"
import { reveal, useEnterAnimation } from "#/lib/motion"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

const podcastLocales = ["zh-hant", "zh-hans", "en"] as const
type PodcastFiles = Record<Locale, File | null>
type ExpectedVersions = Partial<Record<Locale, number>>
type UploadProgress = {
  status: string
  progress: number
  error?: string
  sha256?: string
}
type UploadBatchState = {
  id: string
  tradingDate: string
  reason: PodcastUploadReason
  status: string
  files: Partial<Record<Locale, UploadProgress>>
}
type SelectedPodcastFile = { locale: Locale; file: File }
type ReplacementConfirmation = Readonly<{
  tradingDate: string
  reason: PodcastUploadReason
  files: readonly Readonly<SelectedPodcastFile>[]
  currentVersions: Readonly<ExpectedVersions>
}>
const maxPodcastFileBytes = 256 * 1024 * 1024
const terminalPodcastSessionStatuses = new Set([
  "completed",
  "failed",
  "conflict",
  "expired",
])

function podcastFileMimeType(file: File): string | null {
  const extension = file.name.split(".").pop()?.toLowerCase()
  if (
    extension === "mp3" &&
    ["audio/mpeg", "audio/mp3", ""].includes(file.type)
  ) {
    return "audio/mpeg"
  }
  if (
    extension === "mp4" &&
    ["audio/mp4", "video/mp4", ""].includes(file.type)
  ) {
    return "audio/mp4"
  }
  return null
}

function putPodcastFile(
  file: File,
  target: {
    upload_url: string
    required_headers: Record<string, string>
  },
  onProgress: (progress: number) => void,
  signal: AbortSignal
) {
  return new Promise<number>((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open("PUT", target.upload_url)
    request.withCredentials = false
    for (const [name, value] of Object.entries(target.required_headers)) {
      request.setRequestHeader(name, value)
    }
    request.upload.onprogress = event => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }
    request.onload = () => resolve(request.status)
    request.onerror = () => reject(new Error("network"))
    request.onabort = () => reject(new DOMException("Aborted", "AbortError"))
    signal.addEventListener("abort", () => request.abort(), { once: true })
    request.send(file)
  })
}

export function groupEpisodesByMonth(episodes: PodcastEpisodeAdmin[]) {
  const sorted = [...episodes].sort((left, right) => {
    const dateOrder = right.trading_date.localeCompare(left.trading_date)
    return dateOrder !== 0 ? dateOrder : left.id.localeCompare(right.id)
  })
  const groups = new Map<string, PodcastEpisodeAdmin[]>()
  for (const episode of sorted) {
    const month = episode.trading_date.slice(0, 7)
    const group = groups.get(month)
    if (group) {
      group.push(episode)
    } else {
      groups.set(month, [episode])
    }
  }
  return [...groups].map(([month, monthEpisodes]) => ({
    month,
    episodes: monthEpisodes,
  }))
}

function formatEpisodeMonth(month: string, locale: Locale) {
  return new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${month}-01T00:00:00Z`))
}

export function AudioManagementPage({
  episodes,
  canPublish,
  canEditMetadata = false,
  locale,
}: {
  episodes: PodcastEpisodeAdmin[]
  canPublish: boolean
  canEditMetadata?: boolean
  locale: Locale
}) {
  const { t } = useTranslation()
  const animate = useEnterAnimation()
  const episodeGroups = groupEpisodesByMonth(episodes)
  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("audioManagementTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">
          {t("audioManagementDescription")}
        </p>
      </header>
      <PodcastUploadForm locale={locale} />
      <motion.section
        className="mt-8 grid gap-5"
        aria-labelledby="audio-list-title"
        {...reveal(animate)}
      >
        <h2 className="mb-0 text-2xl" id="audio-list-title">
          {t("audioFiles")}
        </h2>
        {episodes.length === 0 ? (
          <div className="surface-panel border-dashed p-10 text-center">
            <h3 className="mt-0">{t("audioEmptyTitle")}</h3>
            <p className="mb-0 text-sea-ink-soft">
              {t("audioEmptyDescription")}
            </p>
          </div>
        ) : (
          episodeGroups.map(group => (
            <section
              className="grid gap-3"
              key={group.month}
              aria-labelledby={`audio-month-${group.month}`}
            >
              <h3
                className="m-0 border-b border-line pb-2 text-base font-extrabold text-sea-ink-soft"
                id={`audio-month-${group.month}`}
              >
                {formatEpisodeMonth(group.month, locale)}
              </h3>
              <div className="grid gap-3">
                {group.episodes.map(episode => (
                  <EpisodeManager
                    key={episode.id}
                    episode={episode}
                    canPublish={canPublish}
                    canEditMetadata={canEditMetadata}
                    locale={locale}
                  />
                ))}
              </div>
            </section>
          ))
        )}
      </motion.section>
    </main>
  )
}

function PodcastUploadForm({ locale }: { locale: Locale }) {
  const router = useRouter()
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [error, setError] = useState("")
  const [replacementConfirmation, setReplacementConfirmation] =
    useState<ReplacementConfirmation | null>(null)
  const [batch, setBatch] = useState<UploadBatchState | null>(null)
  const [pending, setPending] = useState(false)
  const [preparing, setPreparing] = useState<"hashing" | "initializing" | null>(
    null
  )
  const idempotencyKey = useRef<string | null>(null)
  const abortControllers = useRef<Partial<Record<Locale, AbortController>>>({})
  const uploadTargets = useRef<
    Partial<
      Record<
        Locale,
        {
          upload_url: string
          required_headers: Record<string, string>
          expires_at: string
        }
      >
    >
  >({})
  const batchStatusPoller = useRef<(batchId: string) => void>(() => {})
  const [files, setFiles] = useState<PodcastFiles>({
    "zh-hant": null,
    "zh-hans": null,
    en: null,
  })
  const form = useForm({
    defaultValues: {
      tradingDate: "",
      reason: "initial_upload" as PodcastUploadReason,
    },
    onSubmit: async ({ value }) => startBatch(value, false, {}),
  })

  function setLocaleProgress(locale: Locale, progress: UploadProgress) {
    setBatch(current =>
      current
        ? { ...current, files: { ...current.files, [locale]: progress } }
        : current
    )
  }

  function clearReplacementConfirmation() {
    setReplacementConfirmation(null)
  }

  async function startBatch(
    value: typeof form.state.values,
    confirmReplacement: boolean,
    expectedVersions: ExpectedVersions,
    selectedFiles?: readonly SelectedPodcastFile[]
  ) {
    setError("")
    const selected = selectedFiles
      ? [...selectedFiles]
      : podcastLocales.flatMap(locale => {
          const file = files[locale]
          return file ? [{ locale, file }] : []
        })
    if (selected.length === 0) {
      setError(t("podcastUploadAtLeastOne"))
      return
    }
    for (const { file } of selected) {
      if (file.size === 0 || file.size > maxPodcastFileBytes) {
        setError(t("podcastUploadTooLarge"))
        return
      }
      if (!podcastFileMimeType(file)) {
        setError(t("podcastUploadInvalidType"))
        return
      }
    }
    setPending(true)
    try {
      const csrfToken = await requireCsrfToken()
      setPreparing("hashing")
      const sha256ByLocale: Partial<Record<Locale, string>> = {}
      for (const { locale, file } of selected) {
        const digest = await crypto.subtle.digest(
          "SHA-256",
          await file.arrayBuffer()
        )
        sha256ByLocale[locale] = [...new Uint8Array(digest)]
          .map(byte => byte.toString(16).padStart(2, "0"))
          .join("")
      }
      setPreparing("initializing")
      const key = idempotencyKey.current ?? crypto.randomUUID()
      idempotencyKey.current = key
      const init = await browserPodcastAdminClient().initializeUploadBatch(
        {
          idempotency_key: key,
          trading_date: value.tradingDate,
          reason: value.reason,
          files: selected.map(({ locale, file }) => ({
            locale,
            filename: file.name,
            size_bytes: file.size,
            mime_type: podcastFileMimeType(file)!,
            sha256: sha256ByLocale[locale]!,
            ...(confirmReplacement &&
            typeof expectedVersions[locale] === "number"
              ? {
                  confirm_replacement: true,
                  expected_current_version: expectedVersions[locale],
                }
              : {}),
          })),
        },
        csrfToken
      )
      idempotencyKey.current = null
      setReplacementConfirmation(null)
      uploadTargets.current = Object.fromEntries(
        init.files.map(item => [item.locale, item])
      )
      setBatch({
        id: init.batch_id,
        tradingDate: value.tradingDate,
        reason: value.reason,
        status: init.status,
        files: Object.fromEntries(
          init.files.map(item => [
            item.locale,
            { status: item.status, progress: 0 },
          ])
        ),
      })
      await Promise.all(
        init.files.map(async target => {
          const file = files[target.locale]
          if (!file) return
          const controller = new AbortController()
          abortControllers.current[target.locale] = controller
          setLocaleProgress(target.locale, { status: "uploading", progress: 0 })
          try {
            const putStatus = await putPodcastFile(
              file,
              target,
              progress =>
                setLocaleProgress(target.locale, {
                  status: "uploading",
                  progress,
                }),
              controller.signal
            )
            if (
              putStatus !== 200 &&
              putStatus !== 201 &&
              putStatus !== 204 &&
              putStatus !== 412
            ) {
              throw new Error(putStatus === 0 ? "network" : `http-${putStatus}`)
            }
            await browserPodcastAdminClient().finalizeUploadBatchFile(
              init.batch_id,
              target.locale,
              csrfToken
            )
            setLocaleProgress(target.locale, {
              status: "processing",
              progress: 100,
            })
          } catch (caught) {
            if (
              caught instanceof DOMException &&
              caught.name === "AbortError"
            ) {
              setLocaleProgress(target.locale, {
                status: "cancelled",
                progress: 0,
              })
              return
            }
            if (await redirectExpiredSession(caught)) return
            // A failed PUT may have reached R2. Finalize is safe after 412 and
            // lets the server verify the object before any retry can overwrite it.
            try {
              await browserPodcastAdminClient().finalizeUploadBatchFile(
                init.batch_id,
                target.locale,
                csrfToken
              )
              setLocaleProgress(target.locale, {
                status: "processing",
                progress: 100,
              })
            } catch {
              const reason =
                caught instanceof Error ? caught.message : "network"
              setLocaleProgress(target.locale, {
                status: "failed",
                progress: 0,
                error: reason,
              })
            }
          } finally {
            delete abortControllers.current[target.locale]
          }
        })
      )
      await refreshBatchStatus(init.batch_id, 5)
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        typeof caught.detail === "object" &&
        caught.detail !== null &&
        "code" in caught.detail &&
        ["upload_batch_expired", "idempotency_key_reused"].includes(
          String(caught.detail.code)
        )
      ) {
        const code = String(caught.detail.code)
        idempotencyKey.current = null
        setError(
          t(
            code === "upload_batch_expired"
              ? "podcastUploadNewAttemptRequired"
              : "podcastUploadNewKeyRequired"
          )
        )
        return
      }
      if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        typeof caught.detail === "object" &&
        caught.detail !== null &&
        "code" in caught.detail &&
        caught.detail.code === "replacement_confirmation_required" &&
        "current_versions" in caught.detail &&
        typeof caught.detail.current_versions === "object" &&
        caught.detail.current_versions !== null
      ) {
        setReplacementConfirmation(
          Object.freeze({
            tradingDate: value.tradingDate,
            reason: value.reason,
            files: Object.freeze(
              selected.map(item => Object.freeze({ ...item }))
            ),
            currentVersions: Object.freeze({
              ...(caught.detail.current_versions as ExpectedVersions),
            }),
          })
        )
        idempotencyKey.current = null
        return
      }
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    } finally {
      setPending(false)
      setPreparing(null)
    }
  }

  async function refreshBatchStatus(batchId: string, attempts = 1) {
    try {
      let status = await browserPodcastAdminClient().uploadBatchStatus(batchId)
      const applyStatus = (next: typeof status) => {
        setBatch(current =>
          current?.id === batchId
            ? {
                ...current,
                status: next.status,
                files: {
                  ...current.files,
                  ...Object.fromEntries(
                    next.files.map(item => [
                      item.locale,
                      {
                        status:
                          abortControllers.current[item.locale] &&
                          item.status === "pending_upload"
                            ? (current.files[item.locale]?.status ??
                              item.status)
                            : item.status,
                        progress:
                          abortControllers.current[item.locale] &&
                          item.status === "pending_upload"
                            ? (current.files[item.locale]?.progress ?? 0)
                            : item.status === "completed"
                              ? 100
                              : (current.files[item.locale]?.progress ?? 0),
                        ...(item.sha256 ? { sha256: item.sha256 } : {}),
                        error: item.error_code ?? undefined,
                      },
                    ])
                  ),
                },
              }
            : current
        )
      }
      applyStatus(status)
      for (let attempt = 1; attempt < attempts; attempt += 1) {
        if (
          !status.files.some(item =>
            ["queued", "processing", "pending_upload"].includes(item.status)
          )
        ) {
          break
        }
        await new Promise(resolve => window.setTimeout(resolve, 1000))
        status = await browserPodcastAdminClient().uploadBatchStatus(batchId)
        applyStatus(status)
      }
      if (status.files.some(item => item.status === "completed")) {
        setFiles(current => ({
          ...current,
          ...Object.fromEntries(
            status.files
              .filter(item => item.status === "completed")
              .map(item => [item.locale, null])
          ),
        }))
        await router.invalidate({ sync: true })
      }
    } catch (caught) {
      await redirectExpiredSession(caught)
    }
  }

  batchStatusPoller.current = batchId => {
    void refreshBatchStatus(batchId)
  }

  useEffect(() => {
    if (
      !batch ||
      Object.values(batch.files).every(
        progress =>
          progress && terminalPodcastSessionStatuses.has(progress.status)
      )
    ) {
      return
    }
    const interval = window.setInterval(
      () => batchStatusPoller.current(batch.id),
      2_000
    )
    return () => window.clearInterval(interval)
  }, [batch])

  async function retryLocale(targetLocale: Locale) {
    const target = uploadTargets.current[targetLocale]
    const file = files[targetLocale]
    if (!batch || !file || !target) return
    setPending(true)
    setError("")
    try {
      const csrfToken = await requireCsrfToken()
      const currentStatus = await browserPodcastAdminClient().uploadBatchStatus(
        batch.id
      )
      const session = currentStatus.files.find(
        item => item.locale === targetLocale
      )
      const resumeExistingSession =
        session?.status === "pending_upload" &&
        Date.parse(target.expires_at) > Date.now()
      const hasBlockingSibling = currentStatus.files.some(item => {
        if (
          item.locale === targetLocale ||
          terminalPodcastSessionStatuses.has(item.status)
        ) {
          return false
        }
        const siblingTarget = uploadTargets.current[item.locale]
        return !(
          item.status === "pending_upload" &&
          siblingTarget &&
          Date.parse(siblingTarget.expires_at) <= Date.now()
        )
      })
      if (!resumeExistingSession && hasBlockingSibling) {
        setError(t("podcastRetryWaitingForSiblings"))
        await refreshBatchStatus(batch.id)
        return
      }
      if (session?.status === "completed") {
        await refreshBatchStatus(batch.id)
        return
      }
      if (
        !session ||
        ["failed", "conflict", "expired"].includes(session.status)
      ) {
        idempotencyKey.current = null
        await startBatch(
          { tradingDate: batch.tradingDate, reason: batch.reason },
          false,
          {},
          [{ locale: targetLocale, file }]
        )
        return
      }
      if (["queued", "processing"].includes(session.status)) {
        await refreshBatchStatus(batch.id)
        return
      }
      if (Date.parse(target.expires_at) <= Date.now()) {
        idempotencyKey.current = null
        await startBatch(
          { tradingDate: batch.tradingDate, reason: batch.reason },
          false,
          {},
          [{ locale: targetLocale, file }]
        )
        return
      }
      const controller = new AbortController()
      abortControllers.current[targetLocale] = controller
      setLocaleProgress(targetLocale, { status: "uploading", progress: 0 })
      let putStatus: number
      try {
        putStatus = await putPodcastFile(
          file,
          target,
          progress =>
            setLocaleProgress(targetLocale, { status: "uploading", progress }),
          controller.signal
        )
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === "AbortError") {
          setLocaleProgress(targetLocale, { status: "cancelled", progress: 0 })
          return
        }
        putStatus = 0
      } finally {
        delete abortControllers.current[targetLocale]
      }
      if (![200, 201, 204, 412, 0].includes(putStatus)) {
        throw new Error(`http-${putStatus}`)
      }
      await browserPodcastAdminClient().finalizeUploadBatchFile(
        batch.id,
        targetLocale,
        csrfToken
      )
      setLocaleProgress(targetLocale, { status: "processing", progress: 100 })
      await refreshBatchStatus(batch.id)
    } catch (caught) {
      if (!(await redirectExpiredSession(caught))) {
        setLocaleProgress(targetLocale, {
          status: "failed",
          progress: 0,
          error: caught instanceof Error ? caught.message : "network",
        })
        setError(
          caught instanceof Error ? caught.message : t("unexpectedError")
        )
      }
    } finally {
      setPending(false)
    }
  }

  const hasNonterminalBatchFiles = Boolean(
    batch &&
    Object.values(batch.files).some(
      progress =>
        progress && !terminalPodcastSessionStatuses.has(progress.status)
    )
  )

  return (
    <form
      className="surface-panel grid gap-4 border-t-[3px] border-t-lagoon p-[clamp(1.25rem,3vw,1.5rem)]"
      aria-labelledby="audio-upload-title"
      onSubmit={event => {
        event.preventDefault()
        void form.handleSubmit()
      }}
    >
      <h2 className="mb-0 text-2xl" id="audio-upload-title">
        {t("podcastUploadTitle")}
      </h2>
      <p className="mt-0 text-sea-ink-soft">{t("podcastUploadDescription")}</p>
      <form.Field name="tradingDate">
        {field => (
          <label>
            {t("podcastTradingDate")}
            <input
              required
              type="date"
              disabled={pending || hasNonterminalBatchFiles}
              value={field.state.value}
              onBlur={field.handleBlur}
              onChange={event => {
                clearReplacementConfirmation()
                field.handleChange(event.target.value)
              }}
            />
          </label>
        )}
      </form.Field>
      <fieldset className="m-0 grid gap-3 border-0 p-0">
        <legend className="mb-3 text-sm font-bold">
          {t("audioLanguageFiles")}
        </legend>
        <div className="grid grid-cols-3 gap-3 max-[42rem]:grid-cols-1">
          {podcastLocales.map(locale => (
            <PodcastFileSlot
              key={locale}
              locale={locale}
              file={files[locale]}
              disabled={pending || hasNonterminalBatchFiles}
              onChange={file => {
                clearReplacementConfirmation()
                setFiles(current => ({ ...current, [locale]: file }))
              }}
            />
          ))}
        </div>
      </fieldset>
      <form.Field name="reason">
        {field => (
          <label>
            {t("podcastAuditReason")}
            <select
              disabled={pending || hasNonterminalBatchFiles}
              value={field.state.value}
              onBlur={field.handleBlur}
              onChange={event => {
                clearReplacementConfirmation()
                field.handleChange(event.target.value as PodcastUploadReason)
              }}
            >
              <option value="initial_upload">
                {t("podcastReasonInitialUpload")}
              </option>
              <option value="update_file">
                {t("podcastReasonUpdateFile")}
              </option>
              <option value="other">{t("podcastReasonOther")}</option>
            </select>
          </label>
        )}
      </form.Field>
      {replacementConfirmation && (
        <div
          className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3"
          role="alert"
        >
          <p className="m-0 text-sm font-bold">
            {t("podcastBatchReplacementWarning", {
              locales: Object.keys(
                replacementConfirmation.currentVersions
              ).join(", "),
            })}
          </p>
          <button
            className="font-bold"
            type="button"
            onClick={() => {
              const attempt = replacementConfirmation
              setReplacementConfirmation(null)
              void startBatch(
                {
                  tradingDate: attempt.tradingDate,
                  reason: attempt.reason,
                },
                true,
                { ...attempt.currentVersions },
                attempt.files
              )
            }}
          >
            {t("podcastConfirmReplacement")}
          </button>
        </div>
      )}
      {error && (
        <p className="m-0 text-sm font-bold text-red-700" role="alert">
          {error}
        </p>
      )}
      {preparing && (
        <p
          className="m-0 text-sm text-sea-ink-soft"
          role="status"
          aria-live="polite"
        >
          {t(
            preparing === "hashing"
              ? "podcastUploadHashing"
              : "podcastUploadInitializing"
          )}
        </p>
      )}
      {batch && (
        <section
          className="grid gap-2 rounded-lg border border-line p-3"
          aria-label={t("podcastBatchProgress")}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong>
              {t("podcastBatchStatus", {
                status: t(`podcastBatch_${batch.status}`),
              })}
            </strong>
            <button
              type="button"
              className="text-sm font-bold"
              onClick={() => void refreshBatchStatus(batch.id)}
            >
              {t("podcastRefreshStatus")}
            </button>
          </div>
          {podcastLocales
            .filter(item => batch.files[item])
            .map(item => {
              const progress = batch.files[item]!
              const retryable = [
                "failed",
                "cancelled",
                "expired",
                "conflict",
                "pending_upload",
              ].includes(progress.status)
              const resumablePendingUpload =
                progress.status === "pending_upload" &&
                !abortControllers.current[item] &&
                Date.parse(uploadTargets.current[item]?.expires_at ?? "") >
                  Date.now()
              const hasNonterminalSibling = podcastLocales.some(sibling => {
                const siblingProgress = batch.files[sibling]
                if (
                  sibling === item ||
                  !siblingProgress ||
                  terminalPodcastSessionStatuses.has(siblingProgress.status)
                ) {
                  return false
                }
                const siblingExpiresAt = Date.parse(
                  uploadTargets.current[sibling]?.expires_at ?? ""
                )
                return !(
                  siblingProgress.status === "pending_upload" &&
                  Number.isFinite(siblingExpiresAt) &&
                  siblingExpiresAt <= Date.now()
                )
              })
              return (
                <div
                  className="grid grid-cols-[minmax(4rem,auto)_1fr_auto] items-center gap-3 text-sm"
                  key={item}
                >
                  <strong>{item}</strong>
                  <div>
                    <div className="flex justify-between gap-2">
                      <span>{t(`podcastUpload_${progress.status}`)}</span>
                      {progress.status === "uploading" && (
                        <span>{progress.progress}%</span>
                      )}
                      {progress.error && (
                        <span className="text-red-700">{progress.error}</span>
                      )}
                      {progress.sha256 && (
                        <span className="font-mono text-xs text-sea-ink-soft">
                          {t("podcastVerifiedSha256", {
                            sha256: progress.sha256,
                          })}
                        </span>
                      )}
                      {retryable &&
                        (!resumablePendingUpload ||
                          progress.status !== "pending_upload") &&
                        hasNonterminalSibling && (
                          <span
                            className="text-xs text-sea-ink-soft"
                            role="status"
                          >
                            {t("podcastRetryWaitingForSiblings")}
                          </span>
                        )}
                    </div>
                    {progress.status === "uploading" && (
                      <progress
                        className="w-full"
                        max={100}
                        value={progress.progress}
                      />
                    )}
                  </div>
                  {progress.status === "uploading" ? (
                    <button
                      type="button"
                      onClick={() => abortControllers.current[item]?.abort()}
                    >
                      {t("podcastUploadCancel")}
                    </button>
                  ) : retryable &&
                    (progress.status !== "pending_upload" ||
                      !abortControllers.current[item]) &&
                    files[item] ? (
                    <button
                      type="button"
                      disabled={
                        pending ||
                        Boolean(abortControllers.current[item]) ||
                        (!resumablePendingUpload && hasNonterminalSibling)
                      }
                      onClick={() => void retryLocale(item)}
                    >
                      {t("podcastUploadRetry")}
                    </button>
                  ) : null}
                </div>
              )
            })}
        </section>
      )}
      {hasNonterminalBatchFiles && (
        <p className="m-0 text-sm text-sea-ink-soft" role="status">
          {t("podcastBatchWaitBeforeNewUpload")}
        </p>
      )}
      <form.Subscribe selector={state => state.isSubmitting}>
        {formPending => (
          <button
            className="primary-action w-fit"
            type="submit"
            disabled={formPending || pending || hasNonterminalBatchFiles}
          >
            {formPending || pending
              ? t("submitting")
              : t("podcastUploadSubmit")}
          </button>
        )}
      </form.Subscribe>
    </form>
  )
}

function PodcastFileSlot({
  locale,
  file,
  disabled = false,
  onChange,
}: {
  locale: Locale
  file: File | null
  disabled?: boolean
  onChange: (file: File | null) => void
}) {
  const { t } = useTranslation()
  const [dragging, setDragging] = useState(false)
  const inputId = `podcast-file-${locale}`

  function receiveDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    if (disabled) return
    const nextFile = event.dataTransfer.files.item(0)
    if (nextFile) onChange(nextFile)
  }

  return (
    <div
      className={
        dragging
          ? "grid min-h-32 place-items-center gap-2 rounded-lg border-2 border-dashed border-lagoon bg-lagoon/10 p-4 text-center"
          : "grid min-h-32 place-items-center gap-2 rounded-lg border-2 border-dashed border-line bg-link-hover p-4 text-center transition-colors hover:border-lagoon"
      }
      onDragEnter={event => {
        event.preventDefault()
        if (!disabled) setDragging(true)
      }}
      onDragOver={event => event.preventDefault()}
      onDragLeave={() => setDragging(false)}
      onDrop={receiveDrop}
    >
      <strong className="text-xs tracking-[0.08em] text-kicker uppercase">
        {locale}
      </strong>
      <input
        className="sr-only"
        id={inputId}
        type="file"
        accept=".mp3,.mp4,audio/mpeg,audio/mp4,video/mp4"
        disabled={disabled}
        onChange={event => onChange(event.target.files?.item(0) ?? null)}
      />
      <label
        className={
          disabled
            ? "max-w-full overflow-hidden text-ellipsis text-sm font-bold text-sea-ink"
            : "max-w-full cursor-pointer overflow-hidden text-ellipsis text-sm font-bold text-sea-ink"
        }
        htmlFor={inputId}
      >
        {file ? file.name : t("podcastUploadSlotPrompt")}
      </label>
      {file && (
        <button
          className="px-2 py-1 text-xs"
          type="button"
          disabled={disabled}
          onClick={() => onChange(null)}
        >
          {t("podcastUploadRemove")}
        </button>
      )}
    </div>
  )
}

function EpisodeManager({
  episode,
  canPublish,
  canEditMetadata,
  locale,
}: {
  episode: PodcastEpisodeAdmin
  canPublish: boolean
  canEditMetadata: boolean
  locale: Locale
}) {
  const router = useRouter()
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const available = new Set(
    episode.audio_variants
      .filter(item => item.is_active)
      .map(item => item.locale)
  )
  const missing = podcastLocales.filter(locale => !available.has(locale))

  async function changePublication() {
    if (
      episode.status === "published" &&
      !window.confirm(t("podcastUnpublishConfirmation"))
    ) {
      return
    }
    setPending(true)
    setError("")
    try {
      const client = browserPodcastAdminClient()
      const input = { expected_version: episode.version }
      if (episode.status === "draft") {
        await client.publish(episode.id, input, await requireCsrfToken())
      } else {
        await client.unpublish(episode.id, input, await requireCsrfToken())
      }
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <article className="surface-panel grid w-fit max-w-full grid-cols-[minmax(0,1fr)_auto] gap-3 border-l-[3px] border-l-transparent p-4 max-[42rem]:w-full max-[42rem]:grid-cols-1">
      <header className="flex items-start justify-between gap-4">
        <div>
          <time
            className="text-xs font-extrabold tracking-[0.06em] text-kicker"
            dateTime={episode.trading_date}
          >
            {episode.trading_date}
          </time>
          <h3 className="mt-1 mb-0 text-lg tracking-[-0.025em]">
            {t("audioEpisodeTitle", { date: episode.trading_date })}
          </h3>
        </div>
        <span
          className={
            episode.status === "published"
              ? "rounded-full bg-market-down/15 px-2.5 py-1 text-xs font-extrabold text-market-down"
              : "rounded-full bg-link-hover px-2.5 py-1 text-xs font-extrabold text-sea-ink-soft"
          }
        >
          {t(
            episode.status === "published" ? "podcastPublished" : "podcastDraft"
          )}
        </span>
      </header>
      <p className="m-0 font-mono text-xs text-sea-ink-soft">
        {t("podcastVersion", { version: episode.version })} ·{" "}
        {t("podcastAudioCount", { count: available.size })} ·{" "}
        {t(`podcastMetadataSource_${episode.metadata_source}`)}
      </p>
      <p className="m-0 text-base font-bold text-sea-ink">
        {episode.metadata.find(item => item.locale === locale)?.title ??
          episode.metadata[0]?.title}
      </p>
      {canEditMetadata && (
        <MetadataEditor
          key={`${episode.metadata_source}:${episode.metadata.map(item => item.title).join("|")}`}
          episode={episode}
          locale={locale}
        />
      )}
      {missing.length > 0 && (
        <p
          className="m-0 rounded-lg border border-market-caution/30 bg-market-caution/10 px-3 py-2 text-sm font-bold text-market-caution"
          role="status"
        >
          {t("podcastMissingLocales", { locales: missing.join(", ") })}
        </p>
      )}
      {canPublish &&
        episode.audio_variants
          .filter(item => item.is_active)
          .map(variant => (
            <ChapterEditor
              key={`${variant.locale}:${variant.version}:${variant.chapters_source}`}
              episode={episode}
              variant={variant}
              locale={locale}
            />
          ))}
      {canPublish && (
        <div className="flex flex-wrap justify-end gap-3 max-[42rem]:justify-start">
          <button
            className="primary-action"
            data-action="publication"
            type="button"
            disabled={pending}
            onClick={() => void changePublication()}
          >
            {episode.status === "draft"
              ? t("podcastPublish")
              : t("podcastUnpublish")}
          </button>
        </div>
      )}
      {error && (
        <p className="m-0 text-sm font-bold text-red-700" role="alert">
          {error}
        </p>
      )}
    </article>
  )
}

// Chapters are edited as plain text lines ("m:ss title") per active audio
// file; markers embedded in the upload are the starting point.
function ChapterEditor({
  episode,
  variant,
  locale,
}: {
  episode: PodcastEpisodeAdmin
  variant: PodcastAudioVariantResponse
  locale: Locale
}) {
  const router = useRouter()
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [text, setText] = useState(() => formatChapterText(variant.chapters))
  const [reason, setReason] = useState("")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const fieldId = `podcast-chapters-${episode.id}-${variant.locale}`
  const reasonId = `${fieldId}-reason`
  const dirty = text.trim() !== formatChapterText(variant.chapters).trim()

  async function save() {
    const parsed = parseChapterText(text, variant.duration_seconds)
    if (!parsed.ok) {
      setError(
        t(`podcastChaptersError_${parsed.code}`, {
          line: parsed.line,
          max: maxPodcastChapters,
        })
      )
      return
    }
    setPending(true)
    setError("")
    try {
      await browserPodcastAdminClient().updateChapters(
        episode.id,
        variant.locale,
        {
          expected_version: episode.version,
          chapters: parsed.chapters,
          reason: reason.trim() || "chapters",
        },
        await requireCsrfToken()
      )
      setReason("")
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <form
      className="col-span-full grid gap-2 border-t border-line pt-3"
      data-chapters-locale={variant.locale}
      onSubmit={event => {
        event.preventDefault()
        void save()
      }}
    >
      <label htmlFor={fieldId}>
        {t("podcastChaptersTitle", { locale: variant.locale })}
        <textarea
          className="min-h-24 font-mono text-sm"
          id={fieldId}
          value={text}
          rows={Math.max(3, text.split("\n").length + 1)}
          spellCheck={false}
          onChange={event => setText(event.target.value)}
        />
      </label>
      <p className="m-0 text-xs leading-5 text-sea-ink-soft">
        {t("podcastChaptersHint")}
      </p>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-sea-ink-soft">
        <span>{t(`podcastChaptersSource_${variant.chapters_source}`)}</span>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-48 flex-1" htmlFor={reasonId}>
          {t("podcastAuditReason")}
          <input
            id={reasonId}
            value={reason}
            maxLength={2000}
            onChange={event => setReason(event.target.value)}
          />
        </label>
        <button
          className="primary-action"
          type="submit"
          disabled={pending || !dirty}
        >
          {t("podcastChaptersSave")}
        </button>
      </div>
      {error && (
        <p className="m-0 text-sm font-bold text-red-700" role="alert">
          {error}
        </p>
      )}
    </form>
  )
}

// Localized title and summary for all three languages.
function MetadataEditor({
  episode,
  locale,
}: {
  episode: PodcastEpisodeAdmin
  locale: Locale
}) {
  const router = useRouter()
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const initial = () =>
    Object.fromEntries(
      podcastLocales.map(code => {
        const item = episode.metadata.find(entry => entry.locale === code)
        return [
          code,
          { title: item?.title ?? "", summary: item?.summary ?? "" },
        ]
      })
    ) as Record<Locale, { title: string; summary: string }>
  const [values, setValues] = useState(initial)
  const [reason, setReason] = useState("")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const [open, setOpen] = useState(false)
  const dirty = JSON.stringify(values) !== JSON.stringify(initial())
  const complete = podcastLocales.every(
    code => values[code].title.trim() && values[code].summary.trim()
  )

  async function save() {
    setPending(true)
    setError("")
    try {
      const metadata: PodcastMetadata[] = podcastLocales.map(code => ({
        locale: code,
        title: values[code].title.trim(),
        summary: values[code].summary.trim(),
      }))
      await browserPodcastAdminClient().update(
        episode.id,
        {
          expected_version: episode.version,
          metadata: { values: metadata },
          reason: reason.trim() || "metadata",
        },
        await requireCsrfToken()
      )
      setReason("")
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="col-span-full grid gap-2 border-t border-line pt-3">
      <button
        className="w-fit px-3 py-1.5 text-xs font-bold"
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        {t("podcastEditMetadata")}
      </button>
      {open && (
        <form
          className="grid gap-3"
          data-metadata-editor={episode.id}
          onSubmit={event => {
            event.preventDefault()
            void save()
          }}
        >
          <p className="m-0 text-xs leading-5 text-sea-ink-soft">
            {t("podcastMetadataHint")}
          </p>
          {podcastLocales.map(code => (
            <fieldset
              key={code}
              className="m-0 grid gap-2 rounded-lg border border-line p-3"
            >
              <legend className="px-1 text-xs font-extrabold text-kicker">
                {code}
              </legend>
              <label htmlFor={`podcast-title-${episode.id}-${code}`}>
                {t("podcastMetadataTitle")}
                <input
                  id={`podcast-title-${episode.id}-${code}`}
                  value={values[code].title}
                  maxLength={300}
                  onChange={event =>
                    setValues(current => ({
                      ...current,
                      [code]: { ...current[code], title: event.target.value },
                    }))
                  }
                />
              </label>
              <label htmlFor={`podcast-summary-${episode.id}-${code}`}>
                {t("podcastMetadataSummary")}
                <textarea
                  className="min-h-20"
                  id={`podcast-summary-${episode.id}-${code}`}
                  value={values[code].summary}
                  maxLength={10000}
                  onChange={event =>
                    setValues(current => ({
                      ...current,
                      [code]: { ...current[code], summary: event.target.value },
                    }))
                  }
                />
              </label>
            </fieldset>
          ))}
          <div className="flex flex-wrap items-end gap-3">
            <label
              className="min-w-48 flex-1"
              htmlFor={`podcast-metadata-reason-${episode.id}`}
            >
              {t("podcastAuditReason")}
              <input
                id={`podcast-metadata-reason-${episode.id}`}
                value={reason}
                maxLength={2000}
                onChange={event => setReason(event.target.value)}
              />
            </label>
            <button
              className="primary-action"
              type="submit"
              disabled={pending || !dirty || !complete}
            >
              {t("podcastSaveMetadata")}
            </button>
          </div>
          {error && (
            <p className="m-0 text-sm font-bold text-red-700" role="alert">
              {error}
            </p>
          )}
        </form>
      )}
    </div>
  )
}
