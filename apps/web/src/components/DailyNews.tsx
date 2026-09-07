import { formatTimestamp } from "#/lib/format"
import type { LatestNews, NewsItem } from "@daily-insights/api-client"
import { motion } from "motion/react"
import { useTranslation } from "react-i18next"
import {
  fadeIn,
  hoverLift,
  reveal,
  springs,
  useEnterAnimation,
} from "#/lib/motion"

export function DailyNewsLoading() {
  const animate = useEnterAnimation()
  return (
    <motion.section
      className="surface-panel animate-pulse p-5"
      role="status"
      aria-live="polite"
      variants={fadeIn}
      initial={animate ? "hidden" : false}
      animate="visible"
    >
      <div className="h-3 w-28 rounded bg-line" />
      <div className="mt-3 h-7 w-56 rounded bg-line" />
      <div className="mt-5 space-y-3">
        <div className="h-20 rounded bg-line" />
        <div className="h-20 rounded bg-line" />
      </div>
    </motion.section>
  )
}

export function DailyNews({
  news,
  eyebrowKey = "dailyNewsEyebrow",
  titleKey = "dailyNewsTitle",
  groupByMarket = true,
}: {
  news: LatestNews | null
  eyebrowKey?: string
  titleKey?: string
  // Market pages carry one market's stories, so headings would only repeat
  // the page title (or expose a stray tag from an older edition).
  groupByMarket?: boolean
}) {
  const { t } = useTranslation()
  const animate = useEnterAnimation()
  const status = news?.status ?? "unavailable"
  return (
    <section className="mt-7" aria-labelledby="daily-news-title">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="eyebrow">{t(eyebrowKey)}</p>
          <h2
            id="daily-news-title"
            className="mt-1 mb-0 text-2xl font-extrabold tracking-[-0.03em] text-sea-ink"
          >
            {t(titleKey)}
          </h2>
        </div>
        {status === "partial" ? null : (
          // A partial edition is shown as a plain list: the story count speaks
          // for itself and a shortfall badge was judged noise.
          <span
            className={`rounded-full border px-2.5 py-1 text-xs font-bold ${status === "complete" ? "border-line text-sea-ink-soft" : "border-market-caution/50 bg-market-caution/10 text-market-caution"}`}
          >
            {t(`dailyNewsStatus_${status}`)}
          </span>
        )}
      </div>
      {news !== null && news.status !== "unavailable" && news.caveat ? (
        // Only the pipeline's own caveat (e.g. a stale edition) is surfaced.
        <p className="mt-0 mb-4 text-xs text-sea-ink-soft">{news.caveat}</p>
      ) : null}
      {news === null ? (
        <div
          className="surface-panel p-5 text-sm text-sea-ink-soft"
          role="status"
        >
          {t("dailyNewsLoadFailed")}
        </div>
      ) : news.status === "unavailable" ? (
        <div className="surface-panel p-5 text-sm text-sea-ink-soft">
          {news.caveat ?? t("dailyNewsUnavailable")}
        </div>
      ) : (
        <NewsGroups news={news} animate={animate} grouped={groupByMarket} />
      )}
    </section>
  )
}

/** Stories grouped by the market the selection stage assigned. Editions
 * generated before that was persisted have no market on any item and render
 * as one flat group without a heading. */
function NewsGroups({
  news,
  animate,
  grouped: groupingEnabled,
}: {
  news: LatestNews
  animate: boolean
  grouped: boolean
}) {
  const { t } = useTranslation()
  const groups = new Map<string | null, NewsItem[]>()
  for (const item of news.items) {
    const key = groupingEnabled ? (item.market ?? null) : null
    groups.set(key, [...(groups.get(key) ?? []), item])
  }
  // Headings only earn their space when there is more than one group; a
  // single-market edition reads as a plain list.
  const grouped = groupingEnabled && groups.size > 1
  let offset = 0
  return (
    <div className="grid gap-6">
      {Array.from(groups.entries()).map(([market, items]) => {
        const start = offset
        offset += items.length
        return (
          <section
            key={market ?? "unassigned"}
            aria-label={
              grouped && market ? t(`newsMarket_${market}`) : undefined
            }
          >
            {grouped && market ? (
              <h3 className="mt-0 mb-3 text-sm font-extrabold tracking-[0.04em] text-sea-ink-soft uppercase">
                {t(`newsMarket_${market}`)}
              </h3>
            ) : null}
            <NewsCards items={items} animate={animate} startIndex={start} />
          </section>
        )
      })}
    </div>
  )
}

function NewsCards({
  items,
  animate,
  startIndex,
}: {
  items: ReadonlyArray<NewsItem>
  animate: boolean
  startIndex: number
}) {
  const { t } = useTranslation()
  return (
    // Two independent stacks so each card sits directly under the previous
    // card of its column instead of on a shared grid row. Below lg the
    // stacks are `contents` and the `order` style restores reading order.
    <div className="grid gap-4 lg:grid-cols-2 lg:items-start">
      {[0, 1].map(column => (
        <div
          key={column}
          className="contents lg:grid lg:content-start lg:gap-4"
        >
          {items
            .map((item, index) => ({ item, index }))
            .filter(({ index }) => index % 2 === column)
            .map(({ item, index }) => (
              <motion.article
                key={item.id}
                className="surface-panel p-5 transition-shadow hover:shadow-[0_16px_34px_rgb(14_20_19/9%)]"
                style={{ order: index }}
                {...reveal(animate, startIndex + index)}
                whileHover={hoverLift}
                transition={springs.snappy}
              >
                <div className="flex items-center justify-between gap-3 text-xs text-sea-ink-soft">
                  <span>{item.source_name}</span>
                  <span
                    aria-label={t("dailyNewsImportance", {
                      count: item.importance,
                    })}
                    className="text-market-caution"
                  >
                    {"★".repeat(item.importance)}
                  </span>
                </div>
                <h3 className="mt-3 mb-2 text-lg font-extrabold leading-6 text-sea-ink">
                  {item.headline}
                </h3>
                <p className="m-0 text-sm leading-6 text-sea-ink-soft">
                  {item.summary}
                </p>
                <div className="mt-4 flex items-center justify-between gap-3 text-xs text-sea-ink-soft">
                  {item.source_published_at ? (
                    <time
                      className="font-mono tabular-nums"
                      dateTime={item.source_published_at}
                    >
                      {formatTimestamp(item.source_published_at)}
                    </time>
                  ) : null}
                  <a
                    className="ml-auto font-bold text-lagoon"
                    href={item.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {t("dailyNewsSourceLink")}
                  </a>
                </div>
              </motion.article>
            ))}
        </div>
      ))}
    </div>
  )
}
