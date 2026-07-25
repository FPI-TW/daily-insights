import {
  ApiError,
  type Locale,
  type PodcastEpisodeAdmin,
  type PodcastUploadReason,
} from "@daily-insights/api-client"
import { useForm } from "@tanstack/react-form"
import { createFileRoute, useRouter } from "@tanstack/react-router"
import { useState, type DragEvent } from "react"
import { useTranslation } from "react-i18next"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import {
  browserPodcastAdminClient,
  getAdminPodcastList,
} from "#/lib/admin-podcasts"
import { requireCsrfToken } from "#/lib/auth"

export const Route = createFileRoute(
  "/$locale/_authenticated/back-office/podcasts"
)({
  loader: () => getAdminPodcastList(),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: PodcastAdminPage,
})

const podcastLocales = ["zh-hant", "zh-hans", "en"] as const
type PodcastFiles = Record<Locale, File | null>
type ExpectedVersions = Partial<Record<Locale, number>>

function PodcastAdminPage() {
  const episodes = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  const { t } = useTranslation()
  return (
    <main className="podcast-admin-page">
      <header>
        <p className="eyebrow">{t("backOffice")}</p>
        <h1>{t("podcastAdminTitle")}</h1>
        <p>{t("podcastAdminDescription")}</p>
      </header>
      <PodcastUploadForm />
      <section className="podcast-admin-list">
        <h2>{t("podcastEpisodes")}</h2>
        {episodes.length === 0 ? (
          <p>{t("podcastEmptyDescription")}</p>
        ) : (
          episodes.map(episode => (
            <EpisodeManager
              key={episode.id}
              episode={episode}
              canPublish={user.system_role === "admin"}
            />
          ))
        )}
      </section>
    </main>
  )
}

function PodcastUploadForm() {
  const router = useRouter()
  const { t } = useTranslation()
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
    const files = Object.fromEntries(
      podcastLocales
        .filter(locale => selectedFiles[locale] !== null)
        .map(locale => [locale, selectedFiles[locale]])
    ) as Partial<Record<Locale, File>>
    if (Object.keys(files).length === 0) {
      setError(t("podcastUploadAtLeastOne"))
      return
    }
    try {
      await browserPodcastAdminClient().upload(
        {
          tradingDate: value.tradingDate,
          reason: value.reason,
          files,
          confirmReplacement,
          expectedVersions,
        },
        await requireCsrfToken()
      )
      form.reset()
      setFiles({ "zh-hant": null, "zh-hans": null, en: null })
      await router.invalidate({ sync: true })
    } catch (caught) {
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
      className="podcast-admin-form podcast-upload-form"
      onSubmit={event => {
        event.preventDefault()
        void form.handleSubmit()
      }}
    >
      <h2>{t("podcastUploadTitle")}</h2>
      <p>{t("podcastUploadDescription")}</p>
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
      <div className="podcast-upload-slots">
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
        <div className="podcast-replacement-warning" role="alert">
          <p>
            {t("podcastBatchReplacementWarning", {
              locales: Object.keys(replacementVersions).join(", "),
            })}
          </p>
          <button
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
      {error && <p role="alert">{error}</p>}
      <form.Subscribe selector={state => state.isSubmitting}>
        {pending => (
          <button type="submit" disabled={pending}>
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
      className="podcast-upload-slot"
      data-dragging={dragging || undefined}
      onDragEnter={event => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragOver={event => event.preventDefault()}
      onDragLeave={() => setDragging(false)}
      onDrop={receiveDrop}
    >
      <strong>{locale}</strong>
      <input
        id={inputId}
        type="file"
        accept=".mp3,.mp4,audio/mpeg,audio/mp4,video/mp4"
        onChange={event => onChange(event.target.files?.item(0) ?? null)}
      />
      <label htmlFor={inputId}>
        {file ? file.name : t("podcastUploadSlotPrompt")}
      </label>
      {file && (
        <button type="button" onClick={() => onChange(null)}>
          {t("podcastUploadRemove")}
        </button>
      )}
    </div>
  )
}

function EpisodeManager({
  episode,
  canPublish,
}: {
  episode: PodcastEpisodeAdmin
  canPublish: boolean
}) {
  const router = useRouter()
  const { t } = useTranslation()
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
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    } finally {
      setPending(false)
    }
  }

  return (
    <article className="podcast-admin-card">
      <header>
        <div>
          <time dateTime={episode.trading_date}>{episode.trading_date}</time>
          <h3>Podcast | {episode.trading_date}</h3>
        </div>
        <span data-status={episode.status}>
          {t(
            episode.status === "published" ? "podcastPublished" : "podcastDraft"
          )}
        </span>
      </header>
      <p>
        {t("podcastVersion", { version: episode.version })} ·{" "}
        {t("podcastAudioCount", { count: available.size })}
      </p>
      {missing.length > 0 && (
        <p className="podcast-locale-warning" role="status">
          {t("podcastMissingLocales", { locales: missing.join(", ") })}
        </p>
      )}
      {canPublish && (
        <div className="podcast-publication-controls">
          <button
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
      {error && <p role="alert">{error}</p>}
    </article>
  )
}
