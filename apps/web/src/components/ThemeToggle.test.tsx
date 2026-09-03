import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ThemeModePicker } from "./ThemeToggle"
import { createI18n } from "#/lib/i18n"

const mediaQueryList = {
  matches: false,
  media: "(prefers-color-scheme: dark)",
  onchange: null,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: vi.fn(),
}

describe("ThemeModePicker", () => {
  afterEach(cleanup)
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.className = ""
    document.documentElement.removeAttribute("data-theme")
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => mediaQueryList)
    )
  })

  it("defaults to light and persists dark when selected", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    render(
      <I18nextProvider i18n={i18n}>
        <ThemeModePicker />
      </I18nextProvider>
    )

    expect(screen.getByRole("radiogroup", { name: "Appearance" })).toBeVisible()
    expect(screen.getByRole("radio", { name: "Light" })).toBeChecked()
    expect(document.documentElement).toHaveClass("light")
    expect(document.documentElement).toHaveAttribute("data-theme", "light")

    fireEvent.click(screen.getByRole("radio", { name: "Dark" }))

    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked()
    expect(window.localStorage.getItem("theme")).toBe("dark")
    expect(document.documentElement).toHaveClass("dark")
    expect(document.documentElement).toHaveAttribute("data-theme", "dark")
  })

  it("restores a stored auto mode and follows the system preference", async () => {
    window.localStorage.setItem("theme", "auto")
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    render(
      <I18nextProvider i18n={i18n}>
        <ThemeModePicker />
      </I18nextProvider>
    )

    expect(screen.getByRole("radio", { name: "Auto" })).toBeChecked()
    expect(document.documentElement).not.toHaveAttribute("data-theme")
    expect(mediaQueryList.addEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function)
    )
  })

  it("localizes the option labels", async () => {
    const i18n = createI18n("zh-hant")
    await i18n.changeLanguage("zh-hant")
    render(
      <I18nextProvider i18n={i18n}>
        <ThemeModePicker />
      </I18nextProvider>
    )

    expect(screen.getByRole("radiogroup", { name: "外觀" })).toBeVisible()
    expect(screen.getByRole("radio", { name: "淺色" })).toBeChecked()
    expect(screen.getByRole("radio", { name: "深色" })).not.toBeChecked()
    expect(screen.getByRole("radio", { name: "自動" })).not.toBeChecked()
  })
})
