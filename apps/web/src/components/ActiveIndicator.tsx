import { motion } from "motion/react"
import { useLayoutEffect, useRef, useState } from "react"
import { springs } from "#/lib/motion"

// Shared "active item" marker for navigation and switch controls. Render it
// once as the first child of a `relative isolate` nav; it finds the sibling
// link carrying aria-current="page", measures it relative to the nav and
// springs to it. Measuring against the nav (not the page) keeps the marker
// correct when a route change also resets the scroll position, which a
// layoutId-based shared element would misread as a vertical jump.

export type IndicatorVariant = "pill" | "chip" | "underline"

const variantClass: Record<IndicatorVariant, string> = {
  // Soft tinted background behind top-level navigation.
  pill: "rounded-md bg-lagoon/10",
  // Solid background for compact segmented controls.
  chip: "rounded-md bg-lagoon-deep",
  // Bottom bar for tab rows.
  underline: "bg-lagoon",
}

type Box = { x: number; y: number; width: number; height: number }

export function ActiveIndicator({
  activeKey,
  variant,
}: {
  // Any value that changes when the active link changes; triggers a
  // re-measure after that render has committed.
  activeKey: string
  variant: IndicatorVariant
}) {
  const ref = useRef<HTMLSpanElement>(null)
  const [box, setBox] = useState<Box | null>(null)
  // The first measurement positions the marker without animating so it
  // does not slide in from the corner on hydration.
  const positioned = useRef(false)

  useLayoutEffect(() => {
    const nav = ref.current?.parentElement
    if (!nav) return
    const measure = () => {
      const active = nav.querySelector<HTMLElement>('[aria-current="page"]')
      if (!active) return
      const navRect = nav.getBoundingClientRect()
      const rect = active.getBoundingClientRect()
      const x = rect.left - navRect.left + nav.scrollLeft
      const next: Box =
        variant === "underline"
          ? {
              x,
              y: rect.bottom - navRect.top + nav.scrollTop - 2,
              width: rect.width,
              height: 2,
            }
          : {
              x,
              y: rect.top - navRect.top + nav.scrollTop,
              width: rect.width,
              height: rect.height,
            }
      setBox(previous =>
        previous &&
        previous.x === next.x &&
        previous.y === next.y &&
        previous.width === next.width &&
        previous.height === next.height
          ? previous
          : next
      )
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(nav)
    for (const child of nav.children) observer.observe(child)
    return () => observer.disconnect()
  }, [activeKey, variant])

  const animateNow = positioned.current
  if (box) positioned.current = true

  return (
    <motion.span
      ref={ref}
      className={`pointer-events-none absolute top-0 left-0 -z-10 ${variantClass[variant]}`}
      initial={false}
      animate={box ? { ...box, opacity: 1 } : { opacity: 0 }}
      transition={animateNow ? springs.snappy : { duration: 0 }}
      aria-hidden="true"
    />
  )
}
