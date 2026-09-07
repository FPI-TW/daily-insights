import type { ReactNode } from "react"

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
  meta,
  children,
}: {
  meta?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="min-w-0">
      {meta ? (
        <div className="mb-3 text-xs text-sea-ink-soft">{meta}</div>
      ) : null}
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
