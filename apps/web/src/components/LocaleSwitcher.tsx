import type { Locale } from "@daily-insights/api-client"
import { Link } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import type { MarketCode } from "#/lib/provisional-reports"
import { ActiveIndicator } from "./ActiveIndicator"

const locales: ReadonlyArray<{ code: Locale; label: string }> = [
  { code: "zh-hant", label: "繁中" },
  { code: "zh-hans", label: "简中" },
  { code: "en", label: "EN" },
]

type LocaleDestination =
  | "customer-login"
  | "customer-reports"
  | "customer-podcasts"
  | "customer-account"
  | "customer-change-password"
  | "admin-login"
  | "admin-audio"
  | "admin-members"
  | "admin-change-password"

const destinations = {
  "customer-login": "/$locale/login",
  "customer-reports": "/$locale/reports",
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
  reportMarketCode,
  fullWidth = false,
}: {
  locale: Locale
  destination: LocaleDestination
  reportMarketCode?: MarketCode | undefined
  // Settings dialog: three equal-width segments filling the row.
  fullWidth?: boolean
}) {
  const { t } = useTranslation()
  return (
    <nav
      aria-label={t("language")}
      className={`relative isolate flex min-h-9 items-center rounded-lg border border-chip-line bg-chip p-1 [&>a]:rounded-md [&>a]:font-extrabold [&>a]:text-sea-ink-soft [&>a]:no-underline [&>a]:transition-colors [&>a[aria-current=page]]:text-white ${
        fullWidth
          ? "w-full [&>a]:min-h-7 [&>a]:flex-1 [&>a]:px-3 [&>a]:py-[5px] [&>a]:text-center [&>a]:text-[11.5px] [&>a:not([aria-current=page])]:hover:text-sea-ink"
          : "shrink-0 [&>a]:px-2 [&>a]:py-1 [&>a]:text-[0.68rem] max-sm:[&>a]:px-1.5"
      }`}
    >
      <ActiveIndicator activeKey={locale} variant="chip" />
      {locales.map(({ code, label }) => {
        const active = locale === code
        const content = <>{label}</>
        return reportMarketCode ? (
          <Link
            key={code}
            to="/$locale/reports/$marketCode"
            params={{ locale: code, marketCode: reportMarketCode }}
            aria-current={active ? "page" : undefined}
          >
            {content}
          </Link>
        ) : (
          <Link
            key={code}
            to={destinations[destination]}
            params={{ locale: code }}
            aria-current={active ? "page" : undefined}
          >
            {content}
          </Link>
        )
      })}
    </nav>
  )
}
