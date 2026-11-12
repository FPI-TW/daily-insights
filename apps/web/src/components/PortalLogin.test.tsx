import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { PortalLogin } from "./PortalLogin"

vi.mock("@tanstack/react-router", () => ({
  useRouter: () => ({ invalidate: vi.fn() }),
}))
vi.mock("./LocaleSwitcher", () => ({ LocaleSwitcher: () => null }))

afterEach(cleanup)

describe("portal login password visibility", () => {
  it.each(["customer", "admin"] as const)(
    "keeps the %s login password hidden until requested",
    portal => {
      render(
        <I18nextProvider i18n={createI18n("en")}>
          <PortalLogin locale="en" portal={portal} />
        </I18nextProvider>
      )

      const password = screen.getByLabelText("Password")
      fireEvent.change(password, { target: { value: "Abcd1234" } })
      expect(password).toHaveAttribute("type", "password")

      const show = screen.getByRole("button", { name: "Show password" })
      expect(show).toHaveAttribute("type", "button")
      fireEvent.click(show)
      expect(password).toHaveAttribute("type", "text")
      expect(password).toHaveValue("Abcd1234")

      fireEvent.click(screen.getByRole("button", { name: "Hide password" }))
      expect(password).toHaveAttribute("type", "password")
    }
  )
})
