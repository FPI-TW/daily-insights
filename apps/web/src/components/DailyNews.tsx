import type { LatestNews } from "@daily-insights/api-client"
import { useTranslation } from "react-i18next"

export function DailyNewsLoading() {
  return (
    <section
      className="surface-panel animate-pulse p-5"
      role="status"
      aria-live="polite"
    >
      <div className="h-3 w-28 rounded bg-line" />
      <div className="mt-3 h-7 w-56 rounded bg-line" />
      <div className="mt-5 space-y-3">
        <div className="h-20 rounded bg-line" />
        <div className="h-20 rounded bg-line" />
      </div>
    </section>
  )
}

export function DailyNews({ news }: { news: LatestNews }) {
  const { t } = useTranslation()
  return (
    <section className="mt-7" aria-labelledby="daily-news-title">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="eyebrow">{t("dailyNewsEyebrow")}</p>
          <h2
            id="daily-news-title"
            className="mt-1 mb-0 text-2xl font-extrabold tracking-[-0.03em] text-sea-ink"
          >
            {t("dailyNewsTitle")}
          </h2>
        </div>
        <span className="rounded-full border border-line px-2.5 py-1 text-xs font-bold text-sea-ink-soft">
          {t(`dailyNewsStatus_${news.status}`)}
        </span>
      </div>
      {news.status === "unavailable" ? (
        <div className="surface-panel p-5 text-sm text-sea-ink-soft">
          {news.caveat ?? t("dailyNewsUnavailable")}
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {news.status === "partial" && news.caveat ? (
            <p className="lg:col-span-2 m-0 border-l-4 border-market-caution bg-market-caution/10 px-4 py-3 text-sm text-sea-ink-soft">
              {news.caveat}
            </p>
          ) : null}
          {news.items.map(item => (
            <article key={item.id} className="surface-panel p-5">
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
                <time dateTime={item.source_published_at ?? undefined}>
                  {item.source_published_at
                    ? new Intl.DateTimeFormat(news.locale, {
                        dateStyle: "medium",
                        timeStyle: "short",
                      }).format(new Date(item.source_published_at))
                    : t("dailyNewsTimeUnknown")}
                </time>
                <a
                  className="font-bold text-lagoon"
                  href={item.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("dailyNewsSourceLink")}
                </a>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
