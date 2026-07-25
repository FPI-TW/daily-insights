import type { Locale } from "@daily-insights/api-client"
import { Link } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

const locales: ReadonlyArray<{ code: Locale; label: string }> = [
  { code: "zh-hant", label: "繁中" },
  { code: "zh-hans", label: "简中" },
  { code: "en", label: "EN" },
]

type LocaleDestination =
  | "customer-login"
  | "customer-podcasts"
  | "customer-change-password"
  | "admin-login"
  | "admin-audio"
  | "admin-change-password"

const destinations = {
  "customer-login": "/$locale/login",
  "customer-podcasts": "/$locale/podcasts",
  "customer-change-password": "/$locale/change-password",
  "admin-login": "/$locale/admin/login",
  "admin-audio": "/$locale/admin/audio",
  "admin-change-password": "/$locale/admin/change-password",
} as const

export function LocaleSwitcher({
  locale,
  destination,
}: {
  locale: Locale
  destination: LocaleDestination
}) {
  const { t } = useTranslation()
  return (
    <nav aria-label={t("language")} className="locale-switcher">
      {locales.map(({ code, label }) => (
        <Link
          key={code}
          to={destinations[destination]}
          params={{ locale: code }}
          aria-current={locale === code ? "page" : undefined}
        >
          {label}
        </Link>
      ))}
    </nav>
  )
}
