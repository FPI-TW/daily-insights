import type { Locale } from "@daily-insights/api-client"
import { Link } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import type { MarketCode } from "#/lib/provisional-reports"
import { ActiveIndicator, useIndicatorGroup } from "./ActiveIndicator"

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
}: {
  locale: Locale
  destination: LocaleDestination
  reportMarketCode?: MarketCode | undefined
}) {
  const { t } = useTranslation()
  const group = useIndicatorGroup()
  return (
    <nav
      aria-label={t("language")}
      className="flex min-h-9 shrink-0 items-center rounded-lg border border-chip-line bg-chip p-1 [&>a]:relative [&>a]:isolate [&>a]:rounded-md [&>a]:px-2 [&>a]:py-1 [&>a]:text-[0.68rem] [&>a]:font-extrabold [&>a]:text-sea-ink-soft [&>a]:no-underline [&>a]:transition-colors [&>a[aria-current=page]]:text-white max-sm:[&>a]:px-1.5"
    >
      {locales.map(({ code, label }) => {
        const active = locale === code
        const content = (
          <>
            {active ? <ActiveIndicator group={group} variant="chip" /> : null}
            {label}
          </>
        )
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
