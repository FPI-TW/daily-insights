import type { LatestNews } from "@daily-insights/api-client"
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
}: {
  news: LatestNews | null
  eyebrowKey?: string
  titleKey?: string
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
        <span
          className={`rounded-full border px-2.5 py-1 text-xs font-bold ${status === "complete" ? "border-line text-sea-ink-soft" : "border-market-caution/50 bg-market-caution/10 text-market-caution"}`}
        >
          {t(`dailyNewsStatus_${status}`)}
        </span>
      </div>
      {news !== null && news.status === "partial" ? (
        // A partial badge must explain itself: the caveat from the pipeline,
        // or at least how many stories made it against the target.
        <p className="mt-0 mb-4 text-xs text-sea-ink-soft">
          {news.caveat ??
            t("dailyNewsPartialExplanation", {
              count: news.items.length,
              target: news.target_items,
            })}
        </p>
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
        // Two independent stacks so each card sits directly under the previous
        // card of its column instead of on a shared grid row. Below lg the
        // stacks are `contents` and the `order` style restores reading order.
        <div className="grid gap-4 lg:grid-cols-2 lg:items-start">
          {[0, 1].map(column => (
            <div
              key={column}
              className="contents lg:grid lg:content-start lg:gap-4"
            >
              {news.items
                .map((item, index) => ({ item, index }))
                .filter(({ index }) => index % 2 === column)
                .map(({ item, index }) => (
                  <motion.article
                    key={item.id}
                    className="surface-panel p-5 transition-shadow hover:shadow-[0_16px_34px_rgb(14_20_19/9%)]"
                    style={{ order: index }}
                    {...reveal(animate, index)}
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
                        <time dateTime={item.source_published_at}>
                          {new Intl.DateTimeFormat(news.locale, {
                            dateStyle: "medium",
                            timeStyle: "short",
                            // The edition is the Taipei day; pinning the zone also
                            // keeps SSR and browser output identical (no hydration
                            // mismatch from differing server and client zones).
                            timeZone: "Asia/Taipei",
                          }).format(new Date(item.source_published_at))}
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
      )}
    </section>
  )
}
