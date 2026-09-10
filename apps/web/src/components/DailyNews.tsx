import { formatTimestamp } from "#/lib/format"
import type { LatestNews, NewsItem } from "@daily-insights/api-client"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { AnimatePresence, motion, useReducedMotion } from "motion/react"
import { useLayoutEffect, useRef, useState } from "react"
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
  // The edition status and the pipeline's fallback notice are not shown:
  // readers get the stories or the unavailable panel, nothing about
  // completeness or which day the edition came from.
  return (
    <section className="mt-7 mb-6" aria-labelledby="daily-news-title">
      <div className="mb-4">
        <p className="eyebrow">{t(eyebrowKey)}</p>
        <h2
          id="daily-news-title"
          className="mt-1 mb-0 text-2xl font-extrabold tracking-[-0.03em] text-sea-ink"
        >
          {t(titleKey)}
        </h2>
      </div>
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
        <PaginatedNewsGroups
          key={`${news.edition_id}:${news.revision}`}
          news={news}
          animate={animate}
          grouped={groupByMarket}
        />
      )}
    </section>
  )
}

const NEWS_PAGE_SIZE = 6

function PaginatedNewsGroups({
  news,
  animate,
  grouped,
}: {
  news: LatestNews
  animate: boolean
  grouped: boolean
}) {
  const { t } = useTranslation()
  const [pageIndex, setPageIndex] = useState(0)
  const [direction, setDirection] = useState<1 | -1>(1)
  const [hasPaginated, setHasPaginated] = useState(false)
  const [viewportHeight, setViewportHeight] = useState<number>()
  const pageRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  const totalPages = Math.max(1, Math.ceil(news.items.length / NEWS_PAGE_SIZE))
  const currentPageIndex = Math.min(pageIndex, totalPages - 1)
  const pageStart = currentPageIndex * NEWS_PAGE_SIZE
  const pageNews = {
    ...news,
    items: news.items.slice(pageStart, pageStart + NEWS_PAGE_SIZE),
  }

  useLayoutEffect(() => {
    const page = pageRef.current
    if (!page) return

    const preserveTallestPage = () => {
      const measuredHeight = Math.ceil(page.getBoundingClientRect().height)
      if (measuredHeight > 0) {
        setViewportHeight(current =>
          current === undefined
            ? measuredHeight
            : Math.max(current, measuredHeight)
        )
      }
    }

    preserveTallestPage()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(preserveTallestPage)
    observer.observe(page)
    return () => observer.disconnect()
  }, [currentPageIndex])

  return (
    <>
      <div className="relative">
        <div
          className="grid overflow-hidden"
          style={
            viewportHeight === undefined
              ? undefined
              : { height: viewportHeight }
          }
        >
          <AnimatePresence initial={false} custom={direction} mode="sync">
            <motion.div
              key={currentPageIndex}
              ref={pageRef}
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
        {totalPages > 1 ? (
          <nav
            className="pointer-events-none absolute inset-y-0 right-0 left-0"
            aria-label={t("dailyNewsPagination")}
          >
            <button
              type="button"
              className="pointer-events-auto absolute top-1/2 left-2 z-10 flex size-11 -translate-y-1/2 items-center justify-center rounded-full border border-line bg-surface/95 text-sea-ink shadow-lg backdrop-blur-sm transition-[color,border-color,transform] hover:scale-105 hover:border-lagoon hover:text-lagoon lg:-left-5"
              aria-label={t("dailyNewsPreviousPage")}
              onClick={() => {
                setHasPaginated(true)
                setDirection(-1)
                setPageIndex(index => (index - 1 + totalPages) % totalPages)
              }}
            >
              <ChevronLeft aria-hidden="true" className="size-5" />
            </button>
            <button
              type="button"
              className="pointer-events-auto absolute top-1/2 right-2 z-10 flex size-11 -translate-y-1/2 items-center justify-center rounded-full border border-line bg-surface/95 text-sea-ink shadow-lg backdrop-blur-sm transition-[color,border-color,transform] hover:scale-105 hover:border-lagoon hover:text-lagoon lg:-right-5"
              aria-label={t("dailyNewsNextPage")}
              onClick={() => {
                setHasPaginated(true)
                setDirection(1)
                setPageIndex(index => (index + 1) % totalPages)
              }}
            >
              <ChevronRight aria-hidden="true" className="size-5" />
            </button>
          </nav>
        ) : null}
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
