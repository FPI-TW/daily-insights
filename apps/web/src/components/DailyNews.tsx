import { formatTimestamp, numberLocales } from "#/lib/format"
import type {
  Locale,
  NewsroomEdition,
  NewsroomItem,
  NewsroomRelatedSymbol,
} from "@daily-insights/api-client"
import { Link } from "@tanstack/react-router"
import { ChevronDown, ChevronLeft, ChevronRight } from "lucide-react"
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
  const { t } = useTranslation()
  return (
    <motion.section
      className="surface-panel mb-6 animate-pulse p-5"
      role="status"
      aria-live="polite"
      aria-label={t("newsroomLoading")}
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
  edition: latest,
  eyebrowKey = "newsroomEyebrow",
  titleKey = "newsroomTitle_global",
}: {
  edition: NewsroomEdition | null
  eyebrowKey?: string
  titleKey?: string
}) {
  const { t, i18n } = useTranslation()
  const animate = useEnterAnimation()
  const scope = `${titleKey}:${i18n.resolvedLanguage}`
  const [retained, setRetained] = useState({ scope, edition: latest })
  // Null means this refresh failed, not an authoritative empty edition.
  // Keep the mounted cards (and their page) only within this market/locale.
  if (
    retained.scope !== scope ||
    (latest !== null && latest !== retained.edition)
  ) {
    setRetained({ scope, edition: latest })
  }
  const edition = latest ?? (retained.scope === scope ? retained.edition : null)
  return (
    <section className="mt-7 mb-6" aria-labelledby="daily-news-title">
      {edition !== null && edition.items.length > 0 ? (
        <PaginatedNews
          key={edition.edition_id}
          edition={edition}
          animate={animate}
          eyebrowKey={eyebrowKey}
          titleKey={titleKey}
        />
      ) : (
        <>
          <DailyNewsHeader eyebrowKey={eyebrowKey} titleKey={titleKey} />
          <div
            className="surface-panel p-5 text-sm text-sea-ink-soft"
            role="status"
          >
            {edition === null ? t("newsroomLoadFailed") : t("newsroomEmpty")}
          </div>
        </>
      )}
    </section>
  )
}

const NEWS_PAGE_SIZE = 6

/** "September 30" in the reader's language; the edition date is a calendar
 * day, so it is pinned to UTC to keep the day from shifting. */
function editionDayLabel(date: string, locale: Locale) {
  return new Intl.DateTimeFormat(numberLocales[locale], {
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`))
}

function DailyNewsHeader({
  eyebrowKey,
  titleKey,
  edition,
  children,
}: {
  eyebrowKey: string
  titleKey: string
  edition?: NewsroomEdition
  children?: ReactNode
}) {
  const { t } = useTranslation()
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div className="min-w-0">
        <p className="eyebrow">{t(eyebrowKey)}</p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2
            id="daily-news-title"
            className="m-0 text-2xl font-extrabold tracking-[-0.03em] text-sea-ink"
          >
            {t(titleKey)}
          </h2>
          {edition && !edition.is_today && edition.edition_date ? (
            <p className="m-0 rounded-full border border-chip-line bg-chip px-3 py-1 text-xs font-bold text-sea-ink-soft">
              {t("newsroomEarlierEdition", {
                date: editionDayLabel(edition.edition_date, edition.locale),
              })}
            </p>
          ) : null}
        </div>
      </div>
      {children}
    </div>
  )
}

function PaginatedNews({
  edition,
  animate,
  eyebrowKey,
  titleKey,
}: {
  edition: NewsroomEdition
  animate: boolean
  eyebrowKey: string
  titleKey: string
}) {
  const { t } = useTranslation()
  const [pageIndex, setPageIndex] = useState(0)
  const [direction, setDirection] = useState<1 | -1>(1)
  const [hasPaginated, setHasPaginated] = useState(false)
  const reduceMotion = useReducedMotion()
  const totalPages = Math.max(
    1,
    Math.ceil(edition.items.length / NEWS_PAGE_SIZE)
  )
  const currentPageIndex = Math.min(pageIndex, totalPages - 1)
  const pageStart = currentPageIndex * NEWS_PAGE_SIZE
  const pageItems = edition.items.slice(pageStart, pageStart + NEWS_PAGE_SIZE)

  return (
    <>
      <DailyNewsHeader
        eyebrowKey={eyebrowKey}
        titleKey={titleKey}
        edition={edition}
      >
        {totalPages > 1 ? (
          <nav
            className="flex shrink-0 gap-2"
            aria-label={t("newsroomPagination")}
          >
            <button
              type="button"
              className="flex size-11 items-center justify-center rounded-full border border-lagoon-deep bg-lagoon-deep text-white shadow-[0_6px_16px_rgb(21_158_132/28%)] transition-[color,background-color,border-color,transform,box-shadow] hover:scale-105 hover:border-palm hover:bg-palm hover:shadow-[0_8px_20px_rgb(21_158_132/34%)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lagoon-deep"
              aria-label={t("newsroomPreviousPage")}
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
              aria-label={t("newsroomNextPage")}
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
            <NewsCards
              items={pageItems}
              locale={edition.locale}
              animate={animate && !hasPaginated}
              startIndex={pageStart}
            />
          </motion.div>
        </AnimatePresence>
      </div>
    </>
  )
}

function NewsCards({
  items,
  locale,
  animate,
  startIndex,
}: {
  items: ReadonlyArray<NewsroomItem>
  locale: Locale
  animate: boolean
  startIndex: number
}) {
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
                <NewsCard item={item} locale={locale} />
              </motion.article>
            ))}
        </div>
      ))}
    </div>
  )
}

function NewsCard({ item, locale }: { item: NewsroomItem; locale: Locale }) {
  const { t } = useTranslation()
  return (
    <>
      {item.stars !== null ? (
        <p
          className="m-0 text-right text-xs text-market-caution"
          aria-label={t("newsroomStars", { count: item.stars })}
        >
          {"★".repeat(item.stars)}
        </p>
      ) : null}
      <h3 className="mt-1 mb-2 text-lg leading-6 font-extrabold text-sea-ink">
        {item.headline}
      </h3>
      <p className="m-0 text-sm leading-6 text-sea-ink-soft">{item.summary}</p>
      <div className="mt-4 rounded-[10px] border border-line-soft bg-lagoon-tint px-4 py-3">
        <h4 className="m-0 text-xs font-extrabold tracking-[0.08em] text-lagoon-deep uppercase">
          {t("newsroomWhyItMatters")}
        </h4>
        <p className="mt-1 mb-0 text-sm leading-6 text-sea-ink">{item.why}</p>
      </div>
      {item.related_symbols.length > 0 ? (
        <RelatedSymbols symbols={item.related_symbols} locale={locale} />
      ) : null}
      {item.sources.length > 0 ? <Sources item={item} /> : null}
    </>
  )
}

function RelatedSymbols({
  symbols,
  locale,
}: {
  symbols: ReadonlyArray<NewsroomRelatedSymbol>
  locale: Locale
}) {
  const { t } = useTranslation()
  return (
    <ul
      className="mt-4 mb-0 flex list-none flex-wrap gap-2 p-0"
      aria-label={t("newsroomRelatedSymbols")}
    >
      {symbols.map(symbol => (
        <li key={symbol.symbol}>
          {symbol.market_code ? (
            <Link
              to="/{-$locale}/reports/$marketCode"
              params={{ locale, marketCode: symbol.market_code }}
              className="inline-flex rounded-full border border-chip-line bg-chip px-3 py-1 text-xs font-bold text-lagoon-deep no-underline transition-colors hover:bg-link-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lagoon-deep"
            >
              {symbol.label}
            </Link>
          ) : (
            <span className="inline-flex rounded-full border border-line-soft px-3 py-1 text-xs font-bold text-sea-ink-soft">
              {symbol.label}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

function Sources({ item }: { item: NewsroomItem }) {
  const { t } = useTranslation()
  return (
    <details className="group mt-4 border-t border-line-soft pt-3 text-xs text-sea-ink-soft">
      <summary className="flex cursor-pointer list-none items-center gap-1 font-bold text-lagoon-deep [&::-webkit-details-marker]:hidden">
        {t("newsroomSources", { count: item.sources.length })}
        <ChevronDown
          aria-hidden="true"
          className="size-4 transition-transform group-open:rotate-180"
        />
      </summary>
      <ul className="mt-2 mb-0 grid list-none gap-2 p-0">
        {item.sources.map(source => (
          <li
            key={source.url}
            className="flex items-baseline justify-between gap-3"
          >
            <a
              className="min-w-0 truncate font-bold text-lagoon"
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              {source.name}
            </a>
            {source.published_at ? (
              <time
                className="shrink-0 font-mono tabular-nums"
                dateTime={source.published_at}
              >
                {formatTimestamp(source.published_at)}
              </time>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  )
}
