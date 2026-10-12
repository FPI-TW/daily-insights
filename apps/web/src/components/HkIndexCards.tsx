import type { IndexLatestBar, Locale } from "@daily-insights/api-client"
import { useTranslation } from "react-i18next"
import {
  directionClass,
  formatChange,
  formatNumber,
  percentageChange,
} from "#/lib/format"
import { indexNameKey, trackedSymbolsForMarket } from "#/lib/indices"

// Display codes as the market quotes them, not Yahoo's ticker spelling.
const displayCodes: Record<string, string> = {
  "^HSI": "HSI",
  "^HSCE": "HSCEI",
  "^HSIL": "HSI VIX",
}

export function HkIndexCardsLoading() {
  const { t } = useTranslation()
  return (
    <div
      className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3"
      role="status"
      aria-live="polite"
      aria-label={t("hkIndexCardsLoading")}
    >
      {trackedSymbolsForMarket("hk_equity").map(symbol => (
        <div
          key={symbol}
          className="surface-panel animate-pulse border-l-4 border-l-line p-5"
        >
          <div className="h-3 w-28 rounded bg-line" />
          <div className="mt-3 h-8 w-40 rounded bg-line" />
          <div className="mt-3 h-3 w-24 rounded bg-line" />
        </div>
      ))}
    </div>
  )
}

export function HkIndexCards({
  bars,
  locale,
}: {
  bars: IndexLatestBar[] | null
  locale: Locale
}) {
  const { t } = useTranslation()
  if (bars === null) {
    return (
      <section className="surface-panel p-5" role="status" aria-live="polite">
        <p className="m-0 text-sm text-sea-ink-soft">
          {t("hkIndexCardsUnavailable")}
        </p>
      </section>
    )
  }
  const bySymbol = new Map(bars.map(bar => [bar.symbol, bar]))
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {trackedSymbolsForMarket("hk_equity").map(symbol => {
        const bar = bySymbol.get(symbol)
        const percent = percentageChange(bar?.close, bar?.previous_close)
        const points =
          bar && percent !== null
            ? Number(bar.close) - Number(bar.previous_close)
            : null
        const change = formatChange(percent, locale, {
          flatLabel: t("reportChangeFlat"),
        })
        const colour = directionClass(change.direction)
        const nameKey = indexNameKey(symbol)
        return (
          <section
            key={symbol}
            className={`surface-panel border-l-4 p-5 ${
              change.direction === "up"
                ? "border-l-market-up"
                : change.direction === "down"
                  ? "border-l-market-down"
                  : "border-l-line"
            }`}
            aria-label={t(nameKey ?? symbol)}
          >
            <p className="eyebrow">
              {t(nameKey ?? symbol)} · {displayCodes[symbol] ?? symbol}
            </p>
            <p className="mt-2 mb-0 font-mono text-3xl font-extrabold tabular-nums text-sea-ink">
              {bar ? formatNumber(bar.close, "index", locale) : "—"}
            </p>
            <p
              className={`mt-1 mb-0 flex gap-3 font-mono text-sm font-bold tabular-nums ${colour}`}
            >
              {change.direction === "flat" ? (
                <span>{change.text}</span>
              ) : (
                <>
                  <span>
                    {points === null
                      ? "—"
                      : formatChange(points, locale, {
                          percent: false,
                          flatLabel: t("reportChangeFlat"),
                        }).text}
                  </span>
                  <span>{change.text}</span>
                </>
              )}
            </p>
          </section>
        )
      })}
    </div>
  )
}
