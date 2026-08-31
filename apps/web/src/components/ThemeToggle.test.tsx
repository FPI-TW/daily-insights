import { fireEvent, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { beforeEach, describe, expect, it, vi } from "vitest"

import ThemeToggle from "./ThemeToggle"
import { createI18n } from "#/lib/i18n"

const mediaQueryList = {
  matches: false,
  media: "(prefers-color-scheme: dark)",
  onchange: null,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: vi.fn(),
}

describe("ThemeToggle", () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.className = ""
    document.documentElement.removeAttribute("data-theme")
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => mediaQueryList)
    )
  })

  it("defaults to light and persists dark when toggled", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    render(
      <I18nextProvider i18n={i18n}>
        <ThemeToggle />
      </I18nextProvider>
    )

    expect(screen.getByRole("button")).toHaveTextContent("Light")
    expect(document.documentElement).toHaveClass("light")
    expect(document.documentElement).toHaveAttribute("data-theme", "light")

    fireEvent.click(
      screen.getByRole("button", {
        name: /theme mode: light/i,
      })
    )

    expect(screen.getByRole("button")).toHaveTextContent("Dark")
    expect(window.localStorage.getItem("theme")).toBe("dark")
    expect(document.documentElement).toHaveClass("dark")
    expect(document.documentElement).toHaveAttribute("data-theme", "dark")
  })

  it("localizes its visible and accessible label", async () => {
    const i18n = createI18n("zh-hant")
    await i18n.changeLanguage("zh-hant")
    render(
      <I18nextProvider i18n={i18n}>
        <ThemeToggle />
      </I18nextProvider>
    )

    const button = screen.getByRole("button", {
      name: "目前為淺色模式。點擊切換模式。",
    })
    expect(button).toHaveTextContent("淺色")
    expect(button).toHaveAttribute("title", "目前為淺色模式。點擊切換模式。")
  })
})
