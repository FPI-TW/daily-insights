import type { Locale } from "@daily-insights/api-client"
import { Link } from "@tanstack/react-router"

const locales: ReadonlyArray<{ code: Locale; label: string }> = [
  { code: "zh-TW", label: "繁中" },
  { code: "zh-CN", label: "简中" },
  { code: "en", label: "EN" },
]

export function LocaleSwitcher({ locale }: { locale: Locale }) {
  return (
    <nav aria-label="Language" className="locale-switcher">
      {locales.map(({ code, label }) => (
        <Link
          key={code}
          to="/$locale"
          params={{ locale: code }}
          aria-current={locale === code ? "page" : undefined}
        >
          {label}
        </Link>
      ))}
    </nav>
  )
}
