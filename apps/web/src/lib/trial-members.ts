import type { Locale } from "@daily-insights/api-client"

export function toTaipeiDateTimeInput(value: string | null): string {
  if (!value) return ""
  return new Date(Date.parse(value) + 8 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 16)
}

export function fromTaipeiDateTimeInput(value: string): string {
  return new Date(`${value}+08:00`).toISOString()
}

export function formatTrialExpiry(
  value: string | null,
  locale: Locale
): string {
  if (!value) return "—"
  return new Intl.DateTimeFormat(locale, {
    timeZone: "Asia/Taipei",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}
