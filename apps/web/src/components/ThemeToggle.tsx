import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

type ThemeMode = "light" | "dark" | "auto"

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

export default function ThemeToggle() {
  const { t } = useTranslation()
  const [mode, setMode] = useState<ThemeMode>("light")

  useEffect(() => {
    const initialMode = getInitialMode()
    setMode(initialMode)
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

  function toggleMode() {
    const nextMode: ThemeMode =
      mode === "light" ? "dark" : mode === "dark" ? "auto" : "light"
    setMode(nextMode)
    applyThemeMode(nextMode)
    window.localStorage.setItem("theme", nextMode)
  }

  const label = t(`themeToggleLabel_${mode}`)
  const modeLabel = t(`themeMode_${mode}`)

  return (
    <button
      type="button"
      onClick={toggleMode}
      aria-label={label}
      title={label}
      className="min-h-9 shrink-0 px-3 py-1.5 text-xs font-extrabold max-sm:px-2"
    >
      <span className="max-sm:sr-only">{modeLabel}</span>
      <span className="hidden text-sm max-sm:inline" aria-hidden="true">
        {mode === "auto" ? "◐" : mode === "dark" ? "◒" : "◑"}
      </span>
    </button>
  )
}
