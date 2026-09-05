import {
  ApiError,
  type Locale,
  type PodcastAudioVariantResponse,
  type PodcastEpisodeAdmin,
  type PodcastUploadReason,
} from "@daily-insights/api-client"
import { useForm } from "@tanstack/react-form"
import { useRouter } from "@tanstack/react-router"
import { motion } from "motion/react"
import { useState, type DragEvent } from "react"
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
  locale,
}: {
  episodes: PodcastEpisodeAdmin[]
  canPublish: boolean
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
  const [replacementVersions, setReplacementVersions] =
    useState<ExpectedVersions | null>(null)
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
    onSubmit: async ({ value }) => runUpload(value, files, false, {}),
  })

  async function runUpload(
    value: typeof form.state.values,
    selectedFiles: PodcastFiles,
    confirmReplacement: boolean,
    expectedVersions: ExpectedVersions
  ) {
    setError("")
    setReplacementVersions(null)
    const uploadFiles = Object.fromEntries(
      podcastLocales
        .filter(locale => selectedFiles[locale] !== null)
        .map(locale => [locale, selectedFiles[locale]])
    ) as Partial<Record<Locale, File>>
    if (Object.keys(uploadFiles).length === 0) {
      setError(t("podcastUploadAtLeastOne"))
      return
    }
    try {
      await browserPodcastAdminClient().upload(
        {
          tradingDate: value.tradingDate,
          reason: value.reason,
          files: uploadFiles,
          confirmReplacement,
          expectedVersions,
        },
        await requireCsrfToken()
      )
      form.reset()
      setFiles({ "zh-hant": null, "zh-hans": null, en: null })
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
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
        setReplacementVersions(
          caught.detail.current_versions as ExpectedVersions
        )
        return
      }
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    }
  }

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
              value={field.state.value}
              onBlur={field.handleBlur}
              onChange={event => field.handleChange(event.target.value)}
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
              onChange={file =>
                setFiles(current => ({ ...current, [locale]: file }))
              }
            />
          ))}
        </div>
      </fieldset>
      <form.Field name="reason">
        {field => (
          <label>
            {t("podcastAuditReason")}
            <select
              value={field.state.value}
              onBlur={field.handleBlur}
              onChange={event =>
                field.handleChange(event.target.value as PodcastUploadReason)
              }
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
      {replacementVersions && (
        <div
          className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3"
          role="alert"
        >
          <p className="m-0 text-sm font-bold">
            {t("podcastBatchReplacementWarning", {
              locales: Object.keys(replacementVersions).join(", "),
            })}
          </p>
          <button
            className="font-bold"
            type="button"
            onClick={() =>
              void runUpload(
                form.state.values,
                files,
                true,
                replacementVersions
              )
            }
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
      <form.Subscribe selector={state => state.isSubmitting}>
        {pending => (
          <button
            className="primary-action w-fit"
            type="submit"
            disabled={pending}
          >
            {pending ? t("submitting") : t("podcastUploadSubmit")}
          </button>
        )}
      </form.Subscribe>
    </form>
  )
}

function PodcastFileSlot({
  locale,
  file,
  onChange,
}: {
  locale: Locale
  file: File | null
  onChange: (file: File | null) => void
}) {
  const { t } = useTranslation()
  const [dragging, setDragging] = useState(false)
  const inputId = `podcast-file-${locale}`

  function receiveDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
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
        setDragging(true)
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
        onChange={event => onChange(event.target.files?.item(0) ?? null)}
      />
      <label
        className="max-w-full cursor-pointer overflow-hidden text-ellipsis text-sm font-bold text-sea-ink"
        htmlFor={inputId}
      >
        {file ? file.name : t("podcastUploadSlotPrompt")}
      </label>
      {file && (
        <button
          className="px-2 py-1 text-xs"
          type="button"
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
  locale,
}: {
  episode: PodcastEpisodeAdmin
  canPublish: boolean
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
        {t("podcastAudioCount", { count: available.size })}
      </p>
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
              key={`${variant.locale}:${variant.version}`}
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
