import type { Locale } from "@daily-insights/api-client"

export const brandTitles = {
  "zh-hant": "廷豐金融晨報智能體",
  "zh-hans": "廷豐金融晨報智能體",
  en: "GEAI Daily Insights",
} as const satisfies Record<Locale, string>

export const defaultBrandTitle = brandTitles["zh-hant"]

export function brandTitleFor(locale: string | undefined) {
  return locale === "en" ? brandTitles.en : defaultBrandTitle
}
