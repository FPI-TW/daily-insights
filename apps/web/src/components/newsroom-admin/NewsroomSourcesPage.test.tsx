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
          language_filter: ["en", "ja"],
          full_text_in_feed: false,
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
})
