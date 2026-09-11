import { formatTimestamp } from "#/lib/format"
import type { LatestNews, NewsItem } from "@daily-insights/api-client"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { AnimatePresence, motion, useReducedMotion } from "motion/react"
import type { ReactNode } from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  fadeIn,
  horizontalPageSlide,
  hoverLift,
  reveal,
  springs,
  useEnterAnimation,
} from "#/lib/motion"

export function DailyNewsLoading() {
  const animate = useEnterAnimation()
  return (
    <motion.section
      className="surface-panel mb-6 animate-pulse p-5"
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
  news: latest,
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
  const { t, i18n } = useTranslation()
  const animate = useEnterAnimation()
  const scope = `${titleKey}:${i18n.resolvedLanguage}`
  const [retained, setRetained] = useState({ scope, news: latest })
  // Null means this refresh failed, not an authoritative empty edition.
  // Keep the mounted cards (and their page) only within this market/locale.
  if (
    retained.scope !== scope ||
    (latest !== null && latest !== retained.news)
  ) {
    setRetained({ scope, news: latest })
  }
  const news = latest ?? (retained.scope === scope ? retained.news : null)
  // The edition status and the pipeline's fallback notice are not shown:
  // readers get the stories or the unavailable panel, nothing about
  // completeness or which day the edition came from.
  return (
    <section className="mt-7 mb-6" aria-labelledby="daily-news-title">
      {news !== null && news.status !== "unavailable" ? (
        <PaginatedNewsGroups
          key={`${news.edition_id}:${news.revision}`}
          news={news}
          animate={animate}
          grouped={groupByMarket}
          eyebrowKey={eyebrowKey}
          titleKey={titleKey}
        />
      ) : (
        <>
          <DailyNewsHeader eyebrowKey={eyebrowKey} titleKey={titleKey} />
          {news === null ? (
            <div
              className="surface-panel p-5 text-sm text-sea-ink-soft"
              role="status"
            >
              {t("dailyNewsUnavailable")}
            </div>
          ) : (
            <div className="surface-panel p-5 text-sm text-sea-ink-soft">
              {news.caveat ?? t("dailyNewsUnavailable")}
            </div>
          )}
        </>
      )}
    </section>
  )
}

const NEWS_PAGE_SIZE = 6

function DailyNewsHeader({
  eyebrowKey,
  titleKey,
  children,
}: {
  eyebrowKey: string
  titleKey: string
  children?: ReactNode
}) {
  const { t } = useTranslation()
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div className="min-w-0">
        <p className="eyebrow">{t(eyebrowKey)}</p>
        <h2
          id="daily-news-title"
          className="mt-1 mb-0 text-2xl font-extrabold tracking-[-0.03em] text-sea-ink"
        >
          {t(titleKey)}
        </h2>
      </div>
      {children}
    </div>
  )
}

function PaginatedNewsGroups({
  news,
  animate,
  grouped,
  eyebrowKey,
  titleKey,
}: {
  news: LatestNews
  animate: boolean
  grouped: boolean
  eyebrowKey: string
  titleKey: string
}) {
  const { t } = useTranslation()
  const [pageIndex, setPageIndex] = useState(0)
  const [direction, setDirection] = useState<1 | -1>(1)
  const [hasPaginated, setHasPaginated] = useState(false)
  const reduceMotion = useReducedMotion()
  const totalPages = Math.max(1, Math.ceil(news.items.length / NEWS_PAGE_SIZE))
  const currentPageIndex = Math.min(pageIndex, totalPages - 1)
  const pageStart = currentPageIndex * NEWS_PAGE_SIZE
  const pageNews = {
    ...news,
    items: news.items.slice(pageStart, pageStart + NEWS_PAGE_SIZE),
  }

  return (
    <>
      <DailyNewsHeader eyebrowKey={eyebrowKey} titleKey={titleKey}>
        {totalPages > 1 ? (
          <nav
            className="flex shrink-0 gap-2"
            aria-label={t("dailyNewsPagination")}
          >
            <button
              type="button"
              className="flex size-11 items-center justify-center rounded-full border border-lagoon-deep bg-lagoon-deep text-white shadow-[0_6px_16px_rgb(21_158_132/28%)] transition-[color,background-color,border-color,transform,box-shadow] hover:scale-105 hover:border-palm hover:bg-palm hover:shadow-[0_8px_20px_rgb(21_158_132/34%)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lagoon-deep"
              aria-label={t("dailyNewsPreviousPage")}
              onClick={() => {
                setHasPaginated(true)
                setDirection(-1)
                setPageIndex(index => (index - 1 + totalPages) % totalPages)
              }}
            >
              <ChevronLeft
                aria-hidden="true"
                className="size-6"
                strokeWidth={2.75}
              />
            </button>
            <button
              type="button"
              className="flex size-11 items-center justify-center rounded-full border border-lagoon-deep bg-lagoon-deep text-white shadow-[0_6px_16px_rgb(21_158_132/28%)] transition-[color,background-color,border-color,transform,box-shadow] hover:scale-105 hover:border-palm hover:bg-palm hover:shadow-[0_8px_20px_rgb(21_158_132/34%)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lagoon-deep"
              aria-label={t("dailyNewsNextPage")}
              onClick={() => {
                setHasPaginated(true)
                setDirection(1)
                setPageIndex(index => (index + 1) % totalPages)
              }}
            >
              <ChevronRight
                aria-hidden="true"
                className="size-6"
                strokeWidth={2.75}
              />
            </button>
          </nav>
        ) : null}
      </DailyNewsHeader>
      <div className="grid overflow-hidden">
        <AnimatePresence initial={false} custom={direction} mode="sync">
          <motion.div
            key={currentPageIndex}
            className="col-start-1 row-start-1 w-full"
            custom={direction}
            variants={horizontalPageSlide}
            {...(reduceMotion
              ? { initial: false }
              : {
                  initial: "enter" as const,
                  animate: "visible" as const,
                  exit: "exit" as const,
                })}
          >
            <NewsGroups
              news={pageNews}
              animate={animate && !hasPaginated}
              grouped={grouped}
            />
          </motion.div>
        </AnimatePresence>
      </div>
    </>
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
