import type { User } from "@daily-insights/api-client"
import type { AnchorHTMLAttributes, ReactNode } from "react"
import { I18nextProvider } from "react-i18next"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { AppShell } from "./AppShell"

const router = vi.hoisted(() => ({
  invalidate: vi.fn(),
  navigate: vi.fn(),
}))
const location = vi.hoisted(() => ({ pathname: "/zh-hant/reports" }))

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
  useLocation: () => location,
  useRouter: () => router,
}))

vi.mock("./ActiveIndicator", () => ({
  ActiveIndicator: ({ activeKey }: { activeKey: string }) => (
    <span data-testid="active-indicator" data-active-key={activeKey} />
  ),
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

beforeEach(() => {
  location.pathname = "/zh-hant/reports"
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      media: "(prefers-color-scheme: dark)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }))
  )
})

afterEach(cleanup)

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

  it("opens the complete legal statement from settings", () => {
    const rendered = render(
      <I18nextProvider i18n={createI18n("zh-hant")}>
        <AppShell locale="zh-hant" user={user} surface="customer">
          <main>Content</main>
        </AppShell>
      </I18nextProvider>
    )

    const view = within(rendered.container)
    fireEvent.click(view.getByRole("button", { name: "設定" }))
    fireEvent.click(view.getByRole("button", { name: "法律聲明" }))

    const dialog = view.getByRole("dialog", {
      name: "法律聲明 (Legal Statement)",
    })
    expect(
      within(dialog).getByText(/歡迎您使用廷豐金融科技平台/)
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole("heading", {
        name: "1. 使用者服務條款 (Terms of Service)",
      })
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole("heading", {
        name: "2. 隱私權政策 (Privacy Policy)",
      })
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole("heading", {
        name: "3. 智財權與侵權通報 (IP & Takedown)",
      })
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole("heading", {
        name: "4. 綜合條款 (Miscellaneous)",
      })
    ).toBeInTheDocument()
  })

  it("remeasures the admin tab highlight after leaving news management", () => {
    location.pathname = "/zh-hant/admin/news-management"
    const view = (content: string) => (
      <I18nextProvider i18n={createI18n("zh-hant")}>
        <AppShell
          locale="zh-hant"
          user={{ ...user, system_role: "admin", organization_id: null }}
          surface="admin"
        >
          <main>{content}</main>
        </AppShell>
      </I18nextProvider>
    )
    const rendered = render(view("News"))

    expect(
      within(rendered.container).getByTestId("active-indicator")
    ).toHaveAttribute(
      "data-active-key",
      "zh-hant:/zh-hant/admin/news-management"
    )

    location.pathname = "/zh-hant/admin/data-management"
    rendered.rerender(view("Data"))

    expect(
      within(rendered.container).getByTestId("active-indicator")
    ).toHaveAttribute(
      "data-active-key",
      "zh-hant:/zh-hant/admin/data-management"
    )
  })
})
