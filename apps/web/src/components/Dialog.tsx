import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef, type ReactNode, type RefObject } from "react"
import { backdrop, dialogPanel } from "#/lib/motion"

const focusableSelector =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

// Modal dialog shared by the settings panel and the account page dialogs:
// dimmed backdrop, animated panel, focus moved inside on open, Tab kept
// inside, Escape and backdrop clicks close. Returning focus to the trigger
// is the caller's job in `onClose`, since only it knows the trigger.
export function Dialog({
  open,
  onClose,
  labelledBy,
  initialFocusRef,
  children,
}: {
  open: boolean
  onClose: () => void
  labelledBy: string
  // Element to focus on open; defaults to the first focusable element.
  initialFocusRef?: RefObject<HTMLElement | null>
  children: ReactNode
}) {
  const panelRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (!open) return

    const panel = panelRef.current
    const initial =
      initialFocusRef?.current ??
      panel?.querySelector<HTMLElement>(focusableSelector)
    initial?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose()
        return
      }
      if (event.key !== "Tab") return

      const focusableElements =
        panel?.querySelectorAll<HTMLElement>(focusableSelector)
      if (!focusableElements?.length) return

      const first = focusableElements[0]
      const last = focusableElements[focusableElements.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener("keydown", handleKeyDown)
    return () => document.removeEventListener("keydown", handleKeyDown)
  }, [open, onClose, initialFocusRef])

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          className="fixed inset-0 z-30 grid place-items-center bg-sea-ink/35 p-4"
          role="presentation"
          onMouseDown={onClose}
          variants={backdrop}
          initial="hidden"
          animate="visible"
          exit="hidden"
        >
          <motion.section
            className="w-full max-w-sm rounded-[13px] border border-line bg-surface p-5 shadow-xl"
            role="dialog"
            aria-modal="true"
            aria-labelledby={labelledBy}
            ref={panelRef}
            onMouseDown={event => event.stopPropagation()}
            variants={dialogPanel}
          >
            {children}
          </motion.section>
        </motion.div>
      ) : null}
    </AnimatePresence>
  )
}
