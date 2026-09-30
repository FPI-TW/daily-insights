export function NewsBadge({
  children,
  tone = "neutral",
}: {
  children: string
  tone?: "neutral" | "caution" | "muted"
}) {
  const toneClass =
    tone === "caution"
      ? "border-market-caution/50 bg-market-caution/10 text-market-caution"
      : tone === "muted"
        ? "border-line bg-link-hover text-sea-ink-soft"
        : "border-chip-line bg-chip text-sea-ink"
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-bold whitespace-nowrap ${toneClass}`}
    >
      {children}
    </span>
  )
}
