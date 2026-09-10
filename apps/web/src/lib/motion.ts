import { MotionGlobalConfig } from "motion/react"
import type { Transition, Variants } from "motion/react"
import { useState, useSyncExternalStore } from "react"

// Single source of truth for the site's motion language. Components only
// reference the presets below so a timing or easing change lands everywhere.
// Route crossfades are driven by the View Transitions API (see styles.css and
// router.tsx) and must stay in sync with `durations.pageExit`/`pageEnter`.

export const durations = {
  fast: 0.15,
  base: 0.25,
  enter: 0.35,
  pageExit: 0.25,
  pageEnter: 0.35,
} as const

export const easings = {
  out: [0.22, 1, 0.36, 1],
  in: [0.4, 0, 1, 1],
} as const

export const springs = {
  snappy: { type: "spring", stiffness: 520, damping: 34, mass: 0.8 },
  tap: { type: "spring", stiffness: 700, damping: 30 },
} as const satisfies Record<string, Transition>

// Only the first `staggerLimit` items are staggered; everything after them
// appears together right after the last staggered item so long lists never
// leave their tail waiting.
export const staggerLimit = 8
export const staggerStep = 0.04

export function staggerDelay(index: number) {
  return Math.min(index, staggerLimit) * staggerStep
}

export const fadeInUp: Variants = {
  hidden: { opacity: 0, y: 12 },
  visible: (index: number = 0) => ({
    opacity: 1,
    y: 0,
    transition: {
      duration: durations.enter,
      ease: easings.out,
      delay: staggerDelay(index),
    },
  }),
}

export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: { duration: durations.enter, ease: easings.out },
  },
}

// Horizontal page transition for compact carousels and paginated card sets.
// Positive direction moves forward (old content exits left); negative moves
// backward. Keeping it here preserves one timing language across the app.
export const horizontalPageSlide: Variants = {
  enter: (direction: number = 1) => ({
    x: direction > 0 ? "100%" : "-100%",
  }),
  visible: {
    x: 0,
    transition: { duration: durations.base, ease: easings.out },
  },
  exit: (direction: number = 1) => ({
    x: direction > 0 ? "-100%" : "100%",
    transition: { duration: durations.base, ease: easings.out },
  }),
}

// Overlay/dialog presets used with AnimatePresence.
export const backdrop: Variants = {
  hidden: { opacity: 0, transition: { duration: durations.fast } },
  visible: { opacity: 1, transition: { duration: durations.base } },
}

export const dialogPanel: Variants = {
  hidden: {
    opacity: 0,
    y: 8,
    scale: 0.98,
    transition: { duration: durations.fast, ease: easings.in },
  },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: durations.base, ease: easings.out },
  },
}

export const toast: Variants = {
  hidden: { opacity: 0, y: 12, transition: { duration: durations.fast } },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: durations.base, ease: easings.out },
  },
}

// Interaction presets. Tap feedback for plain buttons is a global CSS rule in
// styles.css; these are for elements rendered through `motion.*`.
export const hoverLift = { y: -2 } as const
export const pressScale = { scale: 0.97 } as const

const subscribeToNothing = () => () => {}

// Whether a component mounting right now should play its enter animation.
// Server-rendered content is already on screen when it hydrates, so animating
// it in would either hide it until JS arrives or make it flicker. Content
// mounted after hydration (client navigations, loader results, pending
// skeletons, async state) animates in.
//
// React hands useSyncExternalStore the server snapshot (false) during any
// hydration render, including Suspense boundaries that hydrate after the root
// has already mounted under streaming SSR, and the client snapshot (true) for
// mounts that never had server HTML. A module-level "hydrated" flag set from
// the root effect is not enough: the root's effect fires before late
// boundaries hydrate, so their content would flash out and fade back in.
export function useEnterAnimation() {
  const isClientMount = useSyncExternalStore(
    subscribeToNothing,
    () => true,
    () => false
  )
  // Test runs set MotionGlobalConfig.skipAnimations; treating those mounts as
  // already visible keeps synchronous visibility assertions deterministic.
  const [animate] = useState(
    isClientMount && !MotionGlobalConfig.skipAnimations
  )
  return animate
}

// Props for a `motion.*` element that fades up on mount. `index` staggers
// siblings in a list.
export function reveal(animate: boolean, index = 0) {
  return {
    variants: fadeInUp,
    custom: index,
    initial: animate ? "hidden" : false,
    animate: "visible",
  } as const
}
