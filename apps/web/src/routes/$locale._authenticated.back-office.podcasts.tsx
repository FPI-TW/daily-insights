import {
  ApiError,
  type Locale,
  type PodcastEpisodeAdmin,
  type PodcastMetadata,
} from "@daily-insights/api-client"
import { useForm } from "@tanstack/react-form"
import { createFileRoute, useRouter } from "@tanstack/react-router"
import { useState } from "react"
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

function metadataFrom(values: {
  zhTwTitle: string
  zhTwSummary: string
  zhCnTitle: string
  zhCnSummary: string
  enTitle: string
  enSummary: string
}): PodcastMetadata[] {
  return [
    {
      locale: "zh-TW",
      title: values.zhTwTitle,
      summary: values.zhTwSummary,
    },
    {
      locale: "zh-CN",
      title: values.zhCnTitle,
      summary: values.zhCnSummary,
    },
    { locale: "en", title: values.enTitle, summary: values.enSummary },
  ]
}

function metadataValue(
  episode: PodcastEpisodeAdmin,
  locale: Locale,
  field: "title" | "summary"
) {
  return episode.metadata.find(item => item.locale === locale)?.[field] ?? ""
}

const metadataFieldSpecs = [
  ["zhTwTitle", "zh-TW", "title"],
  ["zhTwSummary", "zh-TW", "summary"],
  ["zhCnTitle", "zh-CN", "title"],
  ["zhCnSummary", "zh-CN", "summary"],
  ["enTitle", "en", "title"],
  ["enSummary", "en", "summary"],
] as const

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
      {user.system_role === "admin" && <CreateEpisodeForm />}
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

function CreateEpisodeForm() {
  const router = useRouter()
  const { t } = useTranslation()
  const [error, setError] = useState("")
  const form = useForm({
    defaultValues: {
      tradingDate: "",
      zhTwTitle: "",
      zhTwSummary: "",
      zhCnTitle: "",
      zhCnSummary: "",
      enTitle: "",
      enSummary: "",
      reason: "",
    },
    onSubmit: async ({ value }) => {
      setError("")
      try {
        await browserPodcastAdminClient().create(
          {
            trading_date: value.tradingDate,
            metadata: { values: metadataFrom(value) },
            reason: value.reason,
          },
          await requireCsrfToken()
        )
        form.reset()
        await router.invalidate()
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : t("unexpectedError")
        )
      }
    },
  })

  return (
    <form
      className="podcast-admin-form"
      onSubmit={event => {
        event.preventDefault()
        void form.handleSubmit()
      }}
    >
      <h2>{t("podcastCreateEpisode")}</h2>
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
      <div className="podcast-metadata-grid">
        {metadataFieldSpecs.map(([name, locale, kind]) => (
          <form.Field key={name} name={name}>
            {field => (
              <label>
                {locale} ·
                {kind === "title"
                  ? t("podcastMetadataTitle")
                  : t("podcastMetadataSummary")}
                {kind === "title" ? (
                  <input
                    required
                    value={field.state.value}
                    onBlur={field.handleBlur}
                    onChange={event => field.handleChange(event.target.value)}
                  />
                ) : (
                  <textarea
                    required
                    rows={3}
                    value={field.state.value}
                    onBlur={field.handleBlur}
                    onChange={event => field.handleChange(event.target.value)}
                  />
                )}
              </label>
            )}
          </form.Field>
        ))}
      </div>
      <form.Field name="reason">
        {field => (
          <label>
            {t("podcastAuditReason")}
            <input
              required
              value={field.state.value}
              onBlur={field.handleBlur}
              onChange={event => field.handleChange(event.target.value)}
            />
          </label>
        )}
      </form.Field>
      {error && <p role="alert">{error}</p>}
      <form.Subscribe selector={state => state.isSubmitting}>
        {pending => (
          <button type="submit" disabled={pending}>
            {pending ? t("submitting") : t("podcastCreateEpisode")}
          </button>
        )}
      </form.Subscribe>
    </form>
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
  const [reason, setReason] = useState("")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)

  async function changePublication() {
    setPending(true)
    setError("")
    try {
      const client = browserPodcastAdminClient()
      const input = { expected_version: episode.version, reason }
      if (episode.status === "draft") {
        await client.publish(episode.id, input, await requireCsrfToken())
      } else {
        await client.unpublish(episode.id, input, await requireCsrfToken())
      }
      await router.invalidate()
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
          <h3>{metadataValue(episode, "zh-TW", "title")}</h3>
        </div>
        <span data-status={episode.status}>
          {t(
            episode.status === "published" ? "podcastPublished" : "podcastDraft"
          )}
        </span>
      </header>
      <p>
        {t("podcastVersion", { version: episode.version })} ·
        {t("podcastAudioCount", {
          count: episode.audio_variants.filter(item => item.is_active).length,
        })}
      </p>
      {canPublish && episode.status === "draft" && (
        <EditMetadataForm episode={episode} />
      )}
      <AudioImportForm episode={episode} />
      {canPublish && (
        <div className="podcast-publication-controls">
          <label>
            {t("podcastAuditReason")}
            <input
              required
              value={reason}
              onChange={event => setReason(event.target.value)}
            />
          </label>
          <button
            type="button"
            disabled={pending || reason.trim().length === 0}
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

function EditMetadataForm({ episode }: { episode: PodcastEpisodeAdmin }) {
  const router = useRouter()
  const { t } = useTranslation()
  const [error, setError] = useState("")
  const form = useForm({
    defaultValues: {
      zhTwTitle: metadataValue(episode, "zh-TW", "title"),
      zhTwSummary: metadataValue(episode, "zh-TW", "summary"),
      zhCnTitle: metadataValue(episode, "zh-CN", "title"),
      zhCnSummary: metadataValue(episode, "zh-CN", "summary"),
      enTitle: metadataValue(episode, "en", "title"),
      enSummary: metadataValue(episode, "en", "summary"),
      reason: "",
    },
    onSubmit: async ({ value }) => {
      setError("")
      try {
        await browserPodcastAdminClient().update(
          episode.id,
          {
            expected_version: episode.version,
            metadata: { values: metadataFrom(value) },
            reason: value.reason,
          },
          await requireCsrfToken()
        )
        await router.invalidate()
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : t("unexpectedError")
        )
      }
    },
  })
  return (
    <details>
      <summary>{t("podcastEditMetadata")}</summary>
      <form
        className="podcast-admin-form"
        onSubmit={event => {
          event.preventDefault()
          void form.handleSubmit()
        }}
      >
        <div className="podcast-metadata-grid">
          {metadataFieldSpecs.map(([name, locale, kind]) => (
            <form.Field key={name} name={name}>
              {field => (
                <label>
                  {locale} ·
                  {kind === "title"
                    ? t("podcastMetadataTitle")
                    : t("podcastMetadataSummary")}
                  {kind === "title" ? (
                    <input
                      required
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={event => field.handleChange(event.target.value)}
                    />
                  ) : (
                    <textarea
                      required
                      rows={3}
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={event => field.handleChange(event.target.value)}
                    />
                  )}
                </label>
              )}
            </form.Field>
          ))}
        </div>
        <form.Field name="reason">
          {field => (
            <label>
              {t("podcastAuditReason")}
              <input
                required
                value={field.state.value}
                onBlur={field.handleBlur}
                onChange={event => field.handleChange(event.target.value)}
              />
            </label>
          )}
        </form.Field>
        {error && <p role="alert">{error}</p>}
        <button type="submit">{t("podcastSaveMetadata")}</button>
      </form>
    </details>
  )
}

function AudioImportForm({ episode }: { episode: PodcastEpisodeAdmin }) {
  const router = useRouter()
  const { t } = useTranslation()
  const [error, setError] = useState("")
  const [replacementVersion, setReplacementVersion] = useState<number | null>(
    null
  )
  const form = useForm({
    defaultValues: {
      sourceBucket: "",
      sourceKey: "",
      locale: "zh-TW" as Locale,
      mimeType: "audio/mpeg",
      reason: "",
    },
    onSubmit: async ({ value }) => runImport(value, false, null),
  })

  async function runImport(
    value: typeof form.state.values,
    confirmReplacement: boolean,
    expectedVersion: number | null
  ) {
    setError("")
    try {
      await browserPodcastAdminClient().importAudio(
        episode.id,
        {
          source_bucket: value.sourceBucket,
          source_key: value.sourceKey,
          locale: value.locale,
          expected_mime_type: value.mimeType,
          confirm_replacement: confirmReplacement,
          expected_current_version: expectedVersion,
          reason: value.reason,
        },
        await requireCsrfToken()
      )
      setReplacementVersion(null)
      form.reset()
      await router.invalidate()
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        typeof caught.detail === "object" &&
        caught.detail !== null &&
        "code" in caught.detail &&
        caught.detail.code === "replacement_confirmation_required" &&
        "current_version" in caught.detail &&
        typeof caught.detail.current_version === "number"
      ) {
        setReplacementVersion(caught.detail.current_version)
        return
      }
      setError(caught instanceof Error ? caught.message : t("unexpectedError"))
    }
  }

  return (
    <details>
      <summary>{t("podcastImportAudio")}</summary>
      <form
        className="podcast-admin-form podcast-audio-import"
        onSubmit={event => {
          event.preventDefault()
          void form.handleSubmit()
        }}
      >
        <form.Field name="sourceBucket">
          {field => (
            <label>
              {t("podcastSourceBucket")}
              <input
                required
                value={field.state.value}
                onChange={event => field.handleChange(event.target.value)}
              />
            </label>
          )}
        </form.Field>
        <form.Field name="sourceKey">
          {field => (
            <label>
              {t("podcastSourceKey")}
              <input
                required
                value={field.state.value}
                onChange={event => field.handleChange(event.target.value)}
              />
            </label>
          )}
        </form.Field>
        <form.Field name="locale">
          {field => (
            <label>
              {t("podcastAudioLocale")}
              <select
                value={field.state.value}
                onChange={event =>
                  field.handleChange(event.target.value as Locale)
                }
              >
                <option value="zh-TW">zh-TW</option>
                <option value="zh-CN">zh-CN</option>
                <option value="en">en</option>
              </select>
            </label>
          )}
        </form.Field>
        <form.Field name="mimeType">
          {field => (
            <label>
              {t("podcastMimeType")}
              <select
                value={field.state.value}
                onChange={event => field.handleChange(event.target.value)}
              >
                <option value="audio/mpeg">audio/mpeg (.mp3)</option>
                <option value="audio/mp4">audio/mp4 (.m4a)</option>
                <option value="audio/wav">audio/wav (.wav)</option>
              </select>
            </label>
          )}
        </form.Field>
        <form.Field name="reason">
          {field => (
            <label>
              {t("podcastAuditReason")}
              <input
                required
                value={field.state.value}
                onChange={event => field.handleChange(event.target.value)}
              />
            </label>
          )}
        </form.Field>
        {replacementVersion !== null && (
          <div className="podcast-replacement-warning" role="alert">
            <p>
              {t("podcastReplacementWarning", {
                version: replacementVersion,
              })}
            </p>
            <button
              type="button"
              onClick={() =>
                void runImport(form.state.values, true, replacementVersion)
              }
            >
              {t("podcastConfirmReplacement")}
            </button>
          </div>
        )}
        {error && <p role="alert">{error}</p>}
        <button type="submit">{t("podcastImportAudio")}</button>
      </form>
    </details>
  )
}
