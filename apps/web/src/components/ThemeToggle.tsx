import { useEffect, useSyncExternalStore } from "react"
import { useTranslation } from "react-i18next"

export type ThemeMode = "light" | "dark" | "auto"

const themeModes: ReadonlyArray<ThemeMode> = ["light", "dark", "auto"]

function readStoredMode(): ThemeMode {
  if (typeof window === "undefined") return "light"
  const stored = window.localStorage.getItem("theme")
  return stored === "light" || stored === "dark" || stored === "auto"
    ? stored
    : "light"
}

function applyThemeMode(mode: ThemeMode) {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches
  const resolved = mode === "auto" ? (prefersDark ? "dark" : "light") : mode

  document.documentElement.classList.remove("light", "dark")
  document.documentElement.classList.add(resolved)

  if (mode === "auto") {
    document.documentElement.removeAttribute("data-theme")
  } else {
    document.documentElement.setAttribute("data-theme", mode)
  }

  document.documentElement.style.colorScheme = resolved
}

// One store shared by every picker on the page (header settings dialog and
// the account page), so a change in one is reflected in the other.
let currentMode: ThemeMode = "light"
const listeners = new Set<() => void>()

function subscribe(listener: () => void) {
  // First subscriber after a quiet period re-reads storage, which is also
  // what makes each unit test start from its own localStorage.
  if (listeners.size === 0) currentMode = readStoredMode()
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function setThemeMode(mode: ThemeMode) {
  currentMode = mode
  applyThemeMode(mode)
  window.localStorage.setItem("theme", mode)
  for (const listener of listeners) listener()
}

// Persisted theme mode. The server renders "light"; after hydration the
// stored mode takes over (the inline script in __root already applied the
// matching class before first paint, so nothing flashes).
export function useThemeMode() {
  const mode = useSyncExternalStore(
    subscribe,
    () => currentMode,
    () => "light" as ThemeMode
  )

  useEffect(() => {
    applyThemeMode(mode)
    if (mode !== "auto") return

    const media = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => applyThemeMode("auto")
    media.addEventListener("change", onChange)
    return () => {
      media.removeEventListener("change", onChange)
    }
  }, [mode])

  return { mode, setMode: setThemeMode }
}

// Miniature of each theme. The colours are deliberately literal: a preview
// of the light theme must stay light while the dark theme is active, so
// these are the light/dark token values pinned rather than the live tokens.
const previews: Record<
  ThemeMode,
  { background: string; border?: string; bars: [string, string, string] }
> = {
  light: {
    background: "#f4f7f8",
    bars: ["#159e84", "#dbe4e1", "#dbe4e1"],
  },
  dark: {
    background: "#0e1413",
    border: "#26302e",
    bars: ["#2bc5a7", "#34403d", "#34403d"],
  },
  auto: {
    background: "linear-gradient(105deg, #f4f7f8 0 50%, #0e1413 50% 100%)",
    bars: ["#159e84", "#9fb0ab", "#9fb0ab"],
  },
}

const barWidths = ["60%", "100%", "75%"]

// Three preview cards (light / dark / auto) behaving as one radio group.
// "dialog": three equal columns, 46px thumbnails with three bars (settings
// dialog). "row": three fixed 86px cards, 38px thumbnails with two bars
// (account page setting row).
export function ThemeModePicker({
  variant = "dialog",
}: {
  variant?: "dialog" | "row"
}) {
  const { t } = useTranslation()
  const { mode, setMode } = useThemeMode()
  const row = variant === "row"
  const barCount = row ? 2 : 3

  return (
    <div
      className={
        row
          ? "grid grid-cols-[repeat(3,86px)] gap-[9px] max-sm:grid-cols-3"
          : "grid grid-cols-3 gap-[9px]"
      }
      role="radiogroup"
      aria-label={t("theme")}
    >
      {themeModes.map(option => {
        const selected = option === mode
        const preview = previews[option]
        return (
          <label
            key={option}
            className={`grid cursor-pointer gap-2 rounded-[11px] border bg-surface transition-[border-color,box-shadow] duration-[180ms] ${row ? "p-2.5" : "p-[10px]"} ${
              selected
                ? "border-lagoon-deep shadow-[0_0_0_2px_rgb(21_158_132/28%)]"
                : "border-chip-line hover:border-lagoon-deep/45"
            }`}
          >
            <input
              className="sr-only"
              type="radio"
              name={`theme-mode-${variant}`}
              value={option}
              checked={selected}
              onChange={() => setMode(option)}
            />
            <span
              className={`grid content-start gap-1 rounded-[7px] border border-line p-[7px] ${row ? "h-[38px] max-sm:h-[34px]" : "h-[46px] max-sm:h-10"}`}
              style={{
                background: preview.background,
                borderColor: preview.border,
              }}
              aria-hidden="true"
            >
              {preview.bars.slice(0, barCount).map((color, index) => (
                <span
                  key={index}
                  className="h-[5px] rounded-[3px]"
                  style={{ background: color, width: barWidths[index] }}
                />
              ))}
            </span>
            <span
              className={`text-center text-[11.5px] font-extrabold ${selected ? "text-lagoon-deep" : "text-sea-ink-soft"}`}
            >
              {t(`themeMode_${option}`)}
            </span>
          </label>
        )
      })}
    </div>
  )
}
