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
  | "customer-account"
  | "customer-change-password"
  | "admin-login"
  | "admin-audio"
  | "admin-members"
  | "admin-change-password"

const destinations = {
  "customer-login": "/$locale/login",
  "customer-podcasts": "/$locale/podcasts",
  "customer-account": "/$locale/account",
  "customer-change-password": "/$locale/change-password",
  "admin-login": "/$locale/admin/login",
  "admin-audio": "/$locale/admin/audio",
  "admin-members": "/$locale/admin/members",
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
    <nav
      aria-label={t("language")}
      className="flex items-center rounded-lg min-h-9 border border-chip-line bg-chip p-1 [&>a]:rounded-md [&>a]:px-2 [&>a]:py-1 [&>a]:text-[0.68rem] [&>a]:font-extrabold [&>a]:text-sea-ink-soft [&>a]:no-underline [&>a[aria-current=page]]:bg-lagoon-deep [&>a[aria-current=page]]:text-white"
    >
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
