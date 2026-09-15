import {
  ApiError,
  type DataManagementRun,
  type Locale,
  type NewsProgress,
} from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import { useEffect } from "react"
import { useTranslation } from "react-i18next"
import { browserAdministrationClient } from "#/lib/admin-members"
import {
  formatNewsTime as formatTimestamp,
  newsAdminRetryDelay,
  newsRunState,
  retryNewsAdminGet,
} from "#/lib/news-recovery"

export function NewsAdminLoadError({
  error,
  reload,
  pending = false,
}: {
  error: unknown
  reload: () => void
  pending?: boolean
}) {
  const { t } = useTranslation()
  return (
    <div
      role="alert"
      className="my-4 flex flex-wrap items-center gap-3 rounded-lg border border-line p-4"
    >
      <p className="m-0 text-sea-ink">
        {t(
          error instanceof ApiError && error.status === 403
            ? "newsRecoveryForbidden"
            : "newsManagementLoadFailed"
        )}
      </p>
      {error instanceof ApiError && error.requestId ? (
        <span className="text-sm text-sea-ink-soft">
          {t("requestId", { id: error.requestId })}
        </span>
      ) : null}
      <button
        type="button"
        className="min-h-11 rounded-lg border border-line px-4 py-2 font-bold hover:bg-link-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lagoon-deep"
        onClick={reload}
        disabled={pending}
      >
        {t("newsRecoveryReload")}
      </button>
    </div>
  )
}

export function NewsProgressDetails({
  progress,
  locale,
}: {
  progress: NewsProgress
  locale: Locale
}) {
  const { t } = useTranslation()
  return (
    <div className="mt-3 space-y-2 text-sm text-sea-ink-soft">
      <p className="m-0">
        {t(`newsRecoveryState_${progress.state}`)} ·{" "}
        {t(`newsRecoveryStage_${progress.stage}`)}
      </p>
      <p className="m-0">
        {t("newsRecoveryProgress", {
          candidates: progress.progress.discovered ?? 0,
          summaries: progress.progress.summary ?? 0,
          published: progress.progress.published ?? 0,
        })}
      </p>
      <p className="m-0">
        {t(`newsRecoveryPublication_${progress.publication}`)} ·{" "}
        {t("newsRecoveryAttempt", { count: progress.attempt + 1 })}
      </p>
      {progress.next_retry_at ? (
        <p className="m-0">
          {t("newsRecoveryNextRetry", {
            time: formatTimestamp(progress.next_retry_at, locale),
          })}
        </p>
      ) : null}
      {progress.failures.map((failure, index) => (
        <div
          key={`${failure.scope}:${failure.candidate_id}:${failure.locale}:${index}`}
          className="border-l-2 border-line pl-3"
        >
          <p className="m-0">
            {t(`newsRecoveryAction_${failure.action}`)} ·{" "}
            {t(`newsRecoveryStage_${failure.stage}`)} · {failure.scope}{" "}
            {failure.locale}
          </p>
          <p className="m-0 break-words font-mono">
            {failure.code}
            {failure.request_id
              ? ` · ${t("requestId", { id: failure.request_id })}`
              : ""}
          </p>
        </div>
      ))}
    </div>
  )
}

export function NewsMarketProgress({
  runs,
  markets,
  locale,
  taipeiDate,
}: {
  runs: DataManagementRun[]
  markets: readonly string[]
  locale: Locale
  taipeiDate: string
}) {
  const { t } = useTranslation()
  return (
    <section
      className="mt-6 grid max-w-6xl gap-4 md:grid-cols-3"
      aria-label={t("newsRecoveryMarkets")}
    >
      {markets.map(market => {
        const run = runs.find(
          item =>
            item.edition_date === taipeiDate &&
            (item.operation === "news_all" ||
              item.market_code === market ||
              item.news?.[market])
        )
        const progress = run?.news?.[market]
        const scheduled = `${taipeiDate}T08:00:00+08:00`
        return (
          <article
            key={market}
            className="rounded-xl border border-line bg-surface p-5"
          >
            <h2 className="m-0 text-lg font-bold">
              {t(`newsEdition_${market}`)}
            </h2>
            {run ? (
              <p className="mt-3 text-sm text-sea-ink-soft">
                {t(`newsRecoveryState_${newsRunState(run)}`)}
                {run.scheduled_for
                  ? ` · ${t("newsRecoveryScheduled", { time: formatTimestamp(run.scheduled_for, locale) })}`
                  : ""}
              </p>
            ) : (
              <p className="mt-3 text-sm text-sea-ink-soft">
                {t(
                  Date.now() < new Date(scheduled).getTime()
                    ? "newsRecoveryState_scheduled"
                    : "newsRecoveryMissingRun"
                )}
                {" · "}
                {t("newsRecoveryScheduled", {
                  time: formatTimestamp(scheduled, locale),
                })}
              </p>
            )}
            {progress ? (
              <NewsProgressDetails progress={progress} locale={locale} />
            ) : (
              <p className="mt-3 text-sm text-sea-ink-soft">
                {t("newsRecoveryNotRecorded")}
              </p>
            )}
          </article>
        )
      })}
    </section>
  )
}

export function NewsDependencies({
  locale,
  active,
  redirectExpired,
}: {
  locale: Locale
  active: boolean
  redirectExpired: (error: unknown) => Promise<boolean>
}) {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ["news-admin", "recovery"],
    queryFn: () => browserAdministrationClient().newsRecoveryStatus(),
    retry: retryNewsAdminGet,
    retryDelay: newsAdminRetryDelay,
    refetchInterval: state =>
      state.state.error ? false : active ? 5_000 : false,
  })
  useEffect(() => {
    if (query.error) void redirectExpired(query.error)
  }, [query.error, redirectExpired])
  return (
    <details className="mt-6 max-w-6xl rounded-xl border border-line p-5">
      <summary className="cursor-pointer font-bold">
        {t("newsRecoveryDependencies")}
      </summary>
      {query.isPending ? (
        <div
          className="mt-4 h-20 animate-pulse rounded bg-link-hover"
          role="status"
          aria-label={t("newsManagementLoading")}
        />
      ) : null}
      {query.error ? (
        <NewsAdminLoadError
          error={query.error}
          reload={() => void query.refetch()}
          pending={query.isFetching}
        />
      ) : null}
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {query.data?.dependencies.map(dependency => (
          <div
            key={dependency.scope}
            className="rounded-lg border border-line p-3 text-sm"
          >
            <p className="m-0 break-words font-bold">{dependency.scope}</p>
            <p className="mt-2 mb-0">
              {t(`newsRecoveryDependency_${dependency.state}`, {
                defaultValue: dependency.state,
              })}
            </p>
            {dependency.failure ? (
              <p className="mt-2 mb-0 break-words font-mono">
                {dependency.failure.code}
              </p>
            ) : null}
            {dependency.available_at ? (
              <p className="mt-2 mb-0">
                {t("newsRecoveryNextRetry", {
                  time: formatTimestamp(dependency.available_at, locale),
                })}
              </p>
            ) : null}
            <p className="mt-2 mb-0 text-sea-ink-soft">
              {t("newsRecoveryNewest", {
                time: dependency.newest_article_at
                  ? formatTimestamp(dependency.newest_article_at, locale)
                  : t("newsRecoveryNotRecorded"),
              })}
            </p>
          </div>
        ))}
      </div>
    </details>
  )
}
