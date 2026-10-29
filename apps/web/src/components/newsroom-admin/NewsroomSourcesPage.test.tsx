import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { NewsroomSourcesPage } from "./NewsroomSourcesPage"
import { source } from "./test-fixtures"

const client = vi.hoisted(() => ({
  listSources: vi.fn(),
  createSource: vi.fn(),
  updateSource: vi.fn(),
  deleteSource: vi.fn(),
}))

vi.mock("#/lib/newsroom-admin", async importOriginal => ({
  ...(await importOriginal<typeof import("#/lib/newsroom-admin")>()),
  browserNewsroomAdminClient: () => client,
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => vi.fn().mockResolvedValue(false),
}))

const failing = source({
  id: "50000000-0000-4000-8000-0000000000a2",
  key: "wire",
  name: "Wire",
  health: "unhealthy",
  consecutive_failures: 4,
  last_error_code: "http_503",
  last_success_at: null,
})

function renderPage() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <I18nextProvider i18n={createI18n("en")}>
        <NewsroomSourcesPage locale="en" />
      </I18nextProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  client.listSources.mockResolvedValue({ sources: [source(), failing] })
  client.createSource.mockImplementation(async input => source(input))
  client.updateSource.mockImplementation(async (_, changes) => source(changes))
  client.deleteSource.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("NewsroomSourcesPage", () => {
  it("shows a skeleton first, then each source's health", async () => {
    renderPage()
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading news sources."
    )
    const row = (await screen.findByText("Wire")).closest("tr")!
    expect(within(row).getByText("Unhealthy (no success in 6 h)")).toBeVisible()
    expect(
      within(row).getByText("4 consecutive failures (http_503)")
    ).toBeVisible()
    const healthy = screen.getByText("Reuters").closest("tr")!
    expect(within(healthy).getByText("Healthy")).toBeVisible()
    expect(within(healthy).getByText("42")).toBeVisible()
  })

  it("filters by market and exports the visible list", async () => {
    const taiwan = source({
      id: "50000000-0000-4000-8000-0000000000c1",
      key: "udn",
      name: "UDN",
      markets: ["tw_equity"],
      articles_7d: 9,
    })
    client.listSources.mockResolvedValue({
      sources: [source(), failing, taiwan],
    })
    const click = vi.fn()
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:export")
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined)
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(click)

    renderPage()
    await screen.findByText("Reuters")
    fireEvent.click(screen.getByLabelText("Taiwan equities"))
    expect(screen.getByText("UDN")).toBeVisible()
    expect(screen.queryByText("Reuters")).not.toBeInTheDocument()
    expect(screen.getByText("Showing 1 of 3")).toBeVisible()

    fireEvent.click(screen.getByRole("button", { name: "Export list" }))
    expect(createObjectURL).toHaveBeenCalled()
    expect(click).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:export")
  })

  it("sorts by articles in the last 7 days", async () => {
    const quiet = source({
      id: "50000000-0000-4000-8000-0000000000d1",
      key: "quiet",
      name: "Quiet Wire",
      articles_7d: 1,
    })
    const busy = source({
      id: "50000000-0000-4000-8000-0000000000d2",
      key: "busy",
      name: "Busy Wire",
      articles_7d: 99,
    })
    client.listSources.mockResolvedValue({ sources: [quiet, busy] })
    renderPage()
    await screen.findByText("Quiet Wire")
    const sortArticles = () =>
      screen.getByRole("button", { name: /Articles \(7 days\)/ })
    const leadingName = () => {
      const row = screen.getAllByRole("row")[1]!
      return within(row).getByText(/Wire/).textContent
    }
    fireEvent.click(sortArticles())
    const first = leadingName()
    fireEvent.click(sortArticles())
    const second = leadingName()
    expect(first).not.toEqual(second)
    expect([first, second].sort()).toEqual(["Busy Wire", "Quiet Wire"])
  })

  it("shows an empty state when no source exists", async () => {
    client.listSources.mockResolvedValue({ sources: [] })
    renderPage()
    expect(await screen.findByText("No sources yet.")).toBeVisible()
  })

  it("disables a source with a single field update", async () => {
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Disable Reuters" })
    )
    await waitFor(() =>
      expect(client.updateSource).toHaveBeenCalledWith(
        source().id,
        { enabled: false },
        "csrf"
      )
    )
  })

  it("deletes a source only after confirming", async () => {
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete Reuters" })
    )
    const dialog = screen.getByRole("alertdialog")
    expect(
      within(dialog).getByText("Delete source Reuters?")
    ).toBeInTheDocument()
    expect(
      within(dialog).getByText(/The key “reuters” stays reserved/)
    ).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole("button", { name: "Dismiss" }))
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    expect(client.deleteSource).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "Delete Reuters" }))
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", {
        name: "Delete source",
      })
    )
    await waitFor(() =>
      expect(client.deleteSource).toHaveBeenCalledWith(source().id, "csrf")
    )
    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    )
    expect(client.listSources).toHaveBeenCalledTimes(2)
  })

  it("marks the manual source as system, with no disable or delete", async () => {
    client.listSources.mockResolvedValue({
      sources: [
        source(),
        source({
          id: "50000000-0000-4000-8000-0000000000a3",
          key: "manual",
          name: "Manual",
          kind: "manual",
          health: "pending",
          last_success_at: null,
        }),
      ],
    })
    renderPage()
    expect(
      await screen.findByRole("button", { name: "Edit Manual" })
    ).toBeVisible()
    const row = screen.getByText("Manual").closest("tr")!
    expect(within(row).getByText("System")).toBeVisible()
    expect(within(row).getByText(/never polled/)).toBeVisible()
    expect(
      within(row).queryByText("Awaiting first poll")
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Delete Manual" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Disable Manual" })
    ).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Delete Reuters" })).toBeVisible()
    expect(
      screen.getByRole("button", { name: "Disable Reuters" })
    ).toBeVisible()
  })

  it("validates and creates a source", async () => {
    renderPage()
    await screen.findByText("Reuters")
    fireEvent.click(screen.getByRole("button", { name: "Add source" }))
    const dialog = screen.getByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText("Name"), {
      target: { value: "Nikkei Asia" },
    })
    fireEvent.change(within(dialog).getByLabelText(/^Key/), {
      target: { value: "Nikkei Asia" },
    })
    fireEvent.change(within(dialog).getByLabelText("Hostname"), {
      target: { value: "Asia.Nikkei.com" },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    expect(
      await within(dialog).findByText(/Use lowercase letters, digits/)
    ).toBeInTheDocument()
    expect(client.createSource).not.toHaveBeenCalled()

    fireEvent.change(within(dialog).getByLabelText(/^Key/), {
      target: { value: "nikkei-asia" },
    })
    fireEvent.click(within(dialog).getByLabelText("Taiwan equities"))
    fireEvent.change(within(dialog).getByLabelText(/Language filter/), {
      target: { value: "EN, ja" },
    })
    fireEvent.change(within(dialog).getByLabelText(/Excluded titles/), {
      target: { value: "^Market wrap" },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(client.createSource).toHaveBeenCalledWith(
        {
          key: "nikkei-asia",
          name: "Nikkei Asia",
          kind: "rss",
          url: null,
          hostname: "asia.nikkei.com",
          markets: ["tw_equity"],
          trust_tier: 2,
          weight: 1,
          poll_interval_minutes: 30,
          enabled: true,
          link_pattern: null,
          title_exclude_pattern: "^Market wrap",
          language_filter: ["en", "ja"],
          full_text_in_feed: false,
          options: {},
        },
        "csrf"
      )
    )
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    )
  })

  it("edits only the changed fields", async () => {
    renderPage()
    fireEvent.click(await screen.findByRole("button", { name: "Edit Reuters" }))
    const dialog = screen.getByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText(/^Weight/), {
      target: { value: "2" },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(client.updateSource).toHaveBeenCalledWith(
        source().id,
        { weight: 2 },
        "csrf"
      )
    )
  })

  it("requires href_contains when the kind is html_list", async () => {
    renderPage()
    await screen.findByText("Reuters")
    fireEvent.click(screen.getByRole("button", { name: "Add source" }))
    const dialog = screen.getByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText("Name"), {
      target: { value: "MoneyDJ" },
    })
    fireEvent.change(within(dialog).getByLabelText(/^Key/), {
      target: { value: "moneydj-mb00" },
    })
    fireEvent.change(within(dialog).getByLabelText("Hostname"), {
      target: { value: "www.moneydj.com" },
    })
    fireEvent.change(within(dialog).getByLabelText("Format"), {
      target: { value: "html_list" },
    })
    fireEvent.change(within(dialog).getByLabelText("Feed URL"), {
      target: {
        value: "https://www.moneydj.com/KMDJ/News/NewsRealList.aspx?a=MB00",
      },
    })
    fireEvent.click(within(dialog).getByLabelText("Taiwan equities"))
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    expect(
      await within(dialog).findByText(/html_list sources need a non-empty/)
    ).toBeInTheDocument()
    expect(client.createSource).not.toHaveBeenCalled()

    fireEvent.change(within(dialog).getByLabelText(/Advanced options/), {
      target: {
        value:
          '{\n  "href_contains": "NewsViewer.aspx",\n  "keep_query": true\n}',
      },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(client.createSource).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "html_list",
          options: {
            href_contains: "NewsViewer.aspx",
            keep_query: true,
          },
        }),
        "csrf"
      )
    )
  })
})
