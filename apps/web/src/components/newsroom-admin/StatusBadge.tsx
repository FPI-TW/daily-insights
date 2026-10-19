import { cva, type VariantProps } from "class-variance-authority"
import type { ReactNode } from "react"
import { cn } from "#/lib/utils"

// Shared status chip for the newsroom console. Tones map to the design
// tokens, so light and dark themes come from the CSS variables.
const statusBadge = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-extrabold whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "border-chip-line bg-chip text-sea-ink-soft",
        positive: "border-lagoon/40 bg-lagoon-tint text-palm",
        caution:
          "border-market-caution/50 bg-market-caution/10 text-market-caution",
        danger: "border-destructive/50 bg-destructive/10 text-destructive",
      },
    },
    defaultVariants: { tone: "neutral" },
  }
)

export type StatusTone = NonNullable<VariantProps<typeof statusBadge>["tone"]>

export function StatusBadge({
  tone,
  className,
  children,
}: {
  tone?: StatusTone
  className?: string
  children: ReactNode
}) {
  return (
    <span className={cn(statusBadge({ tone }), className)}>{children}</span>
  )
}
