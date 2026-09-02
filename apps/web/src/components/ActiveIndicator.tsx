import { motion } from "motion/react"
import { useId } from "react"
import { springs } from "#/lib/motion"

// Shared "active item" marker for navigation and switch controls. Render it
// inside the currently active link of a group; when the active link changes
// the marker springs from the old link to the new one instead of jumping.
// The host link must be `relative isolate` so the marker can sit behind
// its text.

export type IndicatorVariant = "pill" | "chip" | "underline"

const variantClass: Record<IndicatorVariant, string> = {
  // Soft tinted background behind top-level navigation.
  pill: "absolute inset-0 -z-10 rounded-md bg-lagoon/10",
  // Solid background for compact segmented controls.
  chip: "absolute inset-0 -z-10 rounded-md bg-lagoon-deep",
  // Bottom bar for tab rows.
  underline: "absolute inset-x-0 -bottom-[2px] -z-10 h-[2px] bg-lagoon",
}

// One id per group so markers only travel between links of the same nav.
export function useIndicatorGroup() {
  return useId()
}

export function ActiveIndicator({
  group,
  variant,
}: {
  group: string
  variant: IndicatorVariant
}) {
  return (
    <motion.span
      className={variantClass[variant]}
      layoutId={group}
      transition={springs.snappy}
      aria-hidden="true"
    />
  )
}
