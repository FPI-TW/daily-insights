import { fireEvent, render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import ThemeToggle from "./ThemeToggle"

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

  it("cycles from the system theme to light and persists the preference", () => {
    render(<ThemeToggle />)

    fireEvent.click(
      screen.getByRole("button", {
        name: /theme mode: auto/i,
      })
    )

    expect(screen.getByRole("button")).toHaveTextContent("Light")
    expect(window.localStorage.getItem("theme")).toBe("light")
    expect(document.documentElement).toHaveClass("light")
    expect(document.documentElement).toHaveAttribute("data-theme", "light")
  })
})
