import type { Locale, Market, MarketCode } from "@daily-insights/api-client"
import type { TFunction } from "i18next"
export type NavMarket = { code: MarketCode; name: string }

export function marketName(market: Market, locale: Locale) {
  if (locale === "zh-hant") return market.name_zh_hant
  if (locale === "zh-hans") return market.name_zh_hans
  return market.name_en
}

/**
 * The customer report tabs and administration rerun controls deliberately use
 * the same display label. Keep the localized market name in one translation
 * family so a rename cannot drift between the two surfaces.
 */
export function marketTabLabel(
  t: TFunction,
  market: Pick<NavMarket, "code" | "name">
) {
  return t(`reportMarket_${market.code}`, { defaultValue: market.name })
}
