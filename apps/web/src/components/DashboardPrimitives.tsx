import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"

export function DashboardPanel({
  title,
  controls,
  children,
}: {
  title: string
  controls?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="@container min-w-0 rounded-2xl border border-line bg-surface p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h3 className="m-0 text-base font-extrabold text-sea-ink">{title}</h3>
        {controls}
      </div>
      {children}
    </section>
  )
}

export function DashboardSection({
  number,
  title,
  meta,
  children,
}: {
  number: string
  title: string
  meta: ReactNode
  children: ReactNode
}) {
  return (
    <section className="min-w-0">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2.5">
          <span className="text-[11px] font-extrabold tracking-[0.14em] text-kicker">
            {number}
          </span>
          <h2 className="m-0 text-lg font-extrabold tracking-tight">{title}</h2>
        </div>
        <div className="text-xs text-sea-ink-soft">{meta}</div>
      </div>
      {children}
    </section>
  )
}

export function DashboardChoices<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: readonly { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="flex flex-wrap items-center gap-1"
    >
      {options.map(option => (
        <button
          type="button"
          key={option.value}
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={`rounded-lg border px-2.5 py-1 text-[11px] font-bold transition-colors duration-120 hover:border-lagoon/55 hover:text-palm ${value === option.value ? "border-lagoon/35 bg-lagoon/14 text-palm" : "border-line bg-surface text-sea-ink-soft"}`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Methodology({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <details className="mt-3 text-xs text-sea-ink-soft">
      <summary className="cursor-pointer font-bold transition-colors duration-120 hover:text-palm focus-visible:outline-2 focus-visible:outline-lagoon">
        {t("methodology")}
      </summary>
      <p className="mt-2 mb-0 max-w-[80ch] leading-5 text-pretty">{children}</p>
    </details>
  )
}
