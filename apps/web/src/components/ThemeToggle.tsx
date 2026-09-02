import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

export type ThemeMode = "light" | "dark" | "auto"

const themeModes: ReadonlyArray<ThemeMode> = ["light", "dark", "auto"]

function getInitialMode(): ThemeMode {
  if (typeof window === "undefined") {
    return "light"
  }

  const stored = window.localStorage.getItem("theme")
  if (stored === "light" || stored === "dark" || stored === "auto") {
    return stored
  }

  return "light"
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

// Owns the persisted theme mode: reads localStorage after mount, applies the
// mode to the document, and follows the OS preference while in "auto".
export function useThemeMode() {
  const [mode, setModeState] = useState<ThemeMode>("light")

  useEffect(() => {
    const initialMode = getInitialMode()
    setModeState(initialMode)
    applyThemeMode(initialMode)
  }, [])

  useEffect(() => {
    if (mode !== "auto") {
      return
    }

    const media = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => applyThemeMode("auto")

    media.addEventListener("change", onChange)
    return () => {
      media.removeEventListener("change", onChange)
    }
  }, [mode])

  function setMode(nextMode: ThemeMode) {
    setModeState(nextMode)
    applyThemeMode(nextMode)
    window.localStorage.setItem("theme", nextMode)
  }

  return { mode, setMode }
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
export function ThemeModePicker() {
  const { t } = useTranslation()
  const { mode, setMode } = useThemeMode()

  return (
    <div
      className="grid grid-cols-3 gap-[9px]"
      role="radiogroup"
      aria-label={t("theme")}
    >
      {themeModes.map(option => {
        const selected = option === mode
        const preview = previews[option]
        return (
          <label
            key={option}
            className={`grid cursor-pointer gap-2 rounded-[11px] border bg-surface p-[10px] transition-[border-color,box-shadow] duration-[180ms] ${
              selected
                ? "border-lagoon-deep shadow-[0_0_0_2px_rgb(21_158_132/28%)]"
                : "border-chip-line hover:border-lagoon-deep/45"
            }`}
          >
            <input
              className="sr-only"
              type="radio"
              name="theme-mode"
              value={option}
              checked={selected}
              onChange={() => setMode(option)}
            />
            <span
              className="grid h-[46px] content-start gap-1 rounded-[7px] border border-line p-[7px] max-sm:h-10"
              style={{
                background: preview.background,
                borderColor: preview.border,
              }}
              aria-hidden="true"
            >
              {preview.bars.map((color, index) => (
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
