import type { User } from "@daily-insights/api-client"
import type { AnchorHTMLAttributes, ReactNode } from "react"
import { I18nextProvider } from "react-i18next"
import { render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { AppShell } from "./AppShell"

const router = vi.hoisted(() => ({
  invalidate: vi.fn(),
  navigate: vi.fn(),
}))

type MockLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  children: ReactNode
  params?: { locale?: string }
  search?: unknown
  to: string
}

vi.mock("@tanstack/react-router", () => ({
  Link: ({
    children,
    params,
    search: _search,
    to,
    ...props
  }: MockLinkProps) => (
    <a href={to.replace("{-$locale}", params?.locale ?? "")} {...props}>
      {children}
    </a>
  ),
  useLocation: () => ({ pathname: "/zh-hant/reports" }),
  useRouter: () => router,
}))

vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => vi.fn(),
}))

const user: User = {
  id: "00000000-0000-4000-8000-000000000003",
  email: "listener@example.test",
  display_name: "Listener",
  system_role: "org_member",
  status: "active",
  must_change_password: false,
  organization_id: "00000000-0000-4000-8000-000000000004",
}

describe("AppShell customer navigation", () => {
  it("renders the updated brand and navigation in the requested order", () => {
    const { container } = render(
      <I18nextProvider i18n={createI18n("zh-hant")}>
        <AppShell locale="zh-hant" user={user} surface="customer">
          <main>Content</main>
        </AppShell>
      </I18nextProvider>
    )

    expect(screen.getByText("廷豐金融晨報智能體")).toBeInTheDocument()
    expect(container.querySelector('img[src="/tf-icon.png"]')).toHaveAttribute(
      "aria-hidden",
      "true"
    )

    const navigation = screen.getByRole("navigation", {
      name: "使用者導覽",
    })
    expect(
      within(navigation)
        .getAllByRole("link")
        .map(link => link.textContent)
    ).toEqual(["晨間報告", "Podcast", "AI科技日報", "帳戶"])

    const aiNewsLink = within(navigation).getByRole("link", {
      name: "AI科技日報",
    })
    expect(aiNewsLink).toHaveAttribute(
      "href",
      "https://geaimarketing.github.io/ai-news-daily/"
    )
    expect(aiNewsLink).toHaveAttribute("target", "_blank")
    expect(aiNewsLink).toHaveAttribute("rel", "noreferrer")
  })

  it("offers admins a direct route to customer reports", () => {
    render(
      <I18nextProvider i18n={createI18n("zh-hant")}>
        <AppShell
          locale="zh-hant"
          user={{ ...user, system_role: "admin", organization_id: null }}
          surface="admin"
        >
          <main>Content</main>
        </AppShell>
      </I18nextProvider>
    )

    expect(screen.getByRole("link", { name: "前往晨間報告" })).toHaveAttribute(
      "href",
      "/zh-hant/reports"
    )
  })
})
