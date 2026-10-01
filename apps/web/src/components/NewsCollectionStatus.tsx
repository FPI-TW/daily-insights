import type { NewsFeedPollSource } from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import { useEffect } from "react"
import { Trans, useTranslation } from "react-i18next"
import { NewsBadge } from "#/components/NewsBadge"
import { NewsAdminLoadError } from "#/components/NewsRecoveryStatus"
import { ResponsiveTable } from "#/components/ResponsiveTable"
import { browserAdministrationClient } from "#/lib/admin-members"
import { formatTimestamp } from "#/lib/format"
import {
  feedPollHealth,
  sortFeedPollSources,
  summarizeFeedPolls,
} from "#/lib/news-collection"
import { newsAdminRetryDelay, retryNewsAdminGet } from "#/lib/news-recovery"

/** Current overnight polling state per feed; each poll overwrites its row. */
export function NewsCollectionStatus({
  redirectExpired,
}: {
  redirectExpired: (error: unknown) => Promise<boolean>
}) {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ["news-admin", "collection"],
    queryFn: () => browserAdministrationClient().newsCollectionStatus(),
    retry: retryNewsAdminGet,
    retryDelay: newsAdminRetryDelay,
    refetchInterval: state => (state.state.error ? false : 60_000),
  })
  useEffect(() => {
    if (query.error) void redirectExpired(query.error)
  }, [query.error, redirectExpired])
  // Same Taipei "YYYY-MM-DD HH:mm" stamps as the candidate table below.
  const time = (value: string | null) => (value ? formatTimestamp(value) : "—")

  const now = query.data ? Date.parse(query.data.as_of) : 0
  const sources = query.data ? sortFeedPollSources(query.data.sources, now) : []
  const summary = summarizeFeedPolls(sources, now)

  // Collapsed by default like the source and model health panel above it, so
  // the curation workspace stays near the top of the page.
  return (
    <details className="mt-6 max-w-6xl rounded-xl border border-line p-5">
      <summary className="cursor-pointer font-bold">
        {t("newsCollectionTitle")}
        <span className="ml-2 inline-flex flex-wrap items-center gap-1.5 align-middle font-normal">
          <NewsBadge tone="muted">{t("newsCollectionCurrent")}</NewsBadge>
          {query.isPending ? (
            <span role="status" aria-live="polite">
              <span className="sr-only">{t("newsCollectionLoading")}</span>
              <span className="inline-block h-5 w-56 animate-pulse rounded-full bg-link-hover align-middle" />
            </span>
          ) : !query.data ? (
            <NewsBadge tone="caution">
              {t("newsCollectionSummaryFailed")}
            </NewsBadge>
          ) : summary.total === 0 ? (
            <NewsBadge tone="muted">
              {t("newsCollectionSummaryEmpty")}
            </NewsBadge>
          ) : (
            <>
              <NewsBadge>
                {`${t("newsCollectionSummarySources")} ${summary.total}`}
              </NewsBadge>
              <NewsBadge tone="muted">
                {`${t("newsCollectionHealth_ok")} ${summary.ok}`}
              </NewsBadge>
              <NewsBadge tone={summary.issues > 0 ? "caution" : "muted"}>
                {`${t("newsCollectionSummaryIssues")} ${summary.issues}`}
              </NewsBadge>
              <NewsBadge tone={summary.never > 0 ? "caution" : "muted"}>
                {`${t("newsCollectionHealth_never")} ${summary.never}`}
              </NewsBadge>
              <NewsBadge tone={summary.gaps > 0 ? "caution" : "muted"}>
                {`${t("newsCollectionColGaps")} ${summary.gaps}`}
              </NewsBadge>
            </>
          )}
        </span>
      </summary>
      <p className="mt-4 mb-0 text-sm leading-6 text-sea-ink-soft">
        {t("newsCollectionDescription")}
      </p>
      {query.data && query.error ? (
        <NewsAdminLoadError
          error={query.error}
          reload={() => void query.refetch()}
          pending={query.isFetching}
        />
      ) : null}
      {query.isPending ? (
        <div
          className="mt-4 h-20 animate-pulse rounded bg-link-hover"
          aria-hidden="true"
        />
      ) : !query.data ? (
        <NewsAdminLoadError
          error={query.error}
          reload={() => void query.refetch()}
          pending={query.isFetching}
        />
      ) : sources.length === 0 ? (
        <p className="mt-4 mb-0 text-sm text-sea-ink-soft">
          {t("newsCollectionEmpty")}
        </p>
      ) : (
        <>
          <p className="mt-2 mb-0 text-xs text-sea-ink-soft">
            {t("newsCollectionAsOf", { time: time(query.data.as_of) })}
          </p>
          <div
            className="mt-3 min-w-0 rounded-md border border-line"
            aria-busy={query.isFetching}
          >
            <ResponsiveTable>
              <thead className="bg-link-hover text-left text-xs text-sea-ink-soft">
                <tr>
                  <th scope="col" className="@lg:w-[32%]!">
                    {t("newsCollectionColSource")}
                  </th>
                  <th scope="col" className="@lg:w-[22%]">
                    {t("newsCollectionColStatus")}
                  </th>
                  <th scope="col" className="@lg:w-[13%]">
                    {t("newsCollectionColLastSuccess")}
                  </th>
                  <th scope="col" className="@lg:w-[8%] @lg:text-right">
                    {t("newsCollectionColCount")}
                  </th>
                  <th scope="col" className="@lg:w-[13%]">
                    {t("newsCollectionColCooldown")}
                  </th>
                  <th scope="col" className="@lg:w-[12%] @lg:text-right">
                    {t("newsCollectionColGaps")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {sources.map(source => (
                  <FeedPollRow
                    key={source.source_key}
                    source={source}
                    now={now}
                    time={time}
                  />
                ))}
              </tbody>
            </ResponsiveTable>
          </div>
        </>
      )}
    </details>
  )
}

function FeedPollRow({
  source,
  now,
  time,
}: {
  source: NewsFeedPollSource
  now: number
  time: (value: string | null) => string
}) {
  const { t } = useTranslation()
  const health = feedPollHealth(source, now)
  return (
    <tr className="border-t border-line align-top first:border-t-0">
      <th scope="row" className="text-left font-semibold text-sea-ink">
        <span className="block wrap-break-word">{source.source_name}</span>
        <span
          className="block truncate font-mono text-xs font-normal text-sea-ink-soft"
          title={source.feed_url}
        >
          {source.feed_url}
        </span>
        <span className="mt-1 flex flex-wrap gap-1">
          {source.poll_group ? (
            <NewsBadge tone="muted">
              {t(`newsCollectionPollGroup_${source.poll_group}`, {
                defaultValue: source.poll_group,
              })}
            </NewsBadge>
          ) : null}
          {source.markets.map(market => (
            <NewsBadge key={market}>{t(`newsEdition_${market}`)}</NewsBadge>
          ))}
          {source.registered ? null : (
            <NewsBadge tone="caution">
              {t("newsCollectionUnregistered")}
            </NewsBadge>
          )}
        </span>
      </th>
      <td data-label={t("newsCollectionColStatus")}>
        <span className="flex flex-wrap items-center gap-1">
          <NewsBadge
            tone={
              health === "ok"
                ? "neutral"
                : health === "never"
                  ? "muted"
                  : "caution"
            }
          >
            {t(`newsCollectionHealth_${health}`)}
          </NewsBadge>
          {source.last_status !== null ? (
            <span className="font-mono text-xs tabular-nums">
              HTTP {source.last_status}
            </span>
          ) : null}
        </span>
        {source.last_error_code ? (
          <span className="mt-1 block font-mono text-xs break-all text-market-caution">
            {source.last_error_code}
          </span>
        ) : null}
        {source.consecutive_failures > 0 ? (
          <span className="mt-1 block text-xs text-sea-ink-soft">
            {t("newsCollectionFailures", {
              count: source.consecutive_failures,
            })}
          </span>
        ) : null}
        <span className="mt-1 block text-xs wrap-normal tabular-nums text-sea-ink-soft">
          <Trans
            i18nKey="newsCollectionLastAttempt"
            values={{ time: time(source.last_attempt_at) }}
            components={{ nowrap: <span className="whitespace-nowrap" /> }}
          />
        </span>
      </td>
      <td
        data-label={t("newsCollectionColLastSuccess")}
        className="font-mono text-xs tabular-nums"
      >
        {time(source.last_success_at)}
      </td>
      <td
        data-label={t("newsCollectionColCount")}
        className="font-mono tabular-nums @lg:text-right"
      >
        {source.last_count ?? "—"}
      </td>
      <td
        data-label={t("newsCollectionColCooldown")}
        className="font-mono text-xs tabular-nums"
      >
        {source.cooldown_until && Date.parse(source.cooldown_until) > now
          ? time(source.cooldown_until)
          : "—"}
      </td>
      <td data-label={t("newsCollectionColGaps")} className="@lg:text-right">
        <span className="font-mono tabular-nums">{source.gap_count}</span>
        {source.gap_count_since ? (
          <span className="block text-xs wrap-normal text-sea-ink-soft">
            <Trans
              i18nKey="newsCollectionGapSince"
              values={{ date: source.gap_count_since }}
              components={{ nowrap: <span className="whitespace-nowrap" /> }}
            />
          </span>
        ) : null}
        {source.last_gap_minutes !== null ? (
          <span className="block text-xs wrap-normal text-sea-ink-soft">
            {t("newsCollectionLastGap", { minutes: source.last_gap_minutes })}
          </span>
        ) : null}
      </td>
    </tr>
  )
}
