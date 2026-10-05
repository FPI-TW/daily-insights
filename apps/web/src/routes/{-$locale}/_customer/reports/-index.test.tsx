import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"

const state = vi.hoisted(() => ({
  router: {},
  hydrated: true,
  functions: new Map<string, ReturnType<typeof vi.fn>>(),
}))
vi.mock("@tanstack/react-router", async importOriginal => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  useHydrated: () => state.hydrated,
  useRouter: () => state.router,
  createFileRoute: () => (options: unknown) => ({
    options,
    useRouteContext: () => ({
      locale: "en",
      user: { id: "u", organization_id: "o" },
    }),
  }),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => vi.fn(),
  isSessionExpiryPending: () => false,
}))
vi.mock("#/lib/market-queries", async importOriginal => ({
  ...(await importOriginal<typeof import("#/lib/market-queries")>()),
  marketQueries: () =>
    Object.fromEntries(
      ["markets", "reports", "news", "viewpoints"].map(key => [
        key,
        () => ({
          queryKey: ["market", "u", "o", key],
          queryFn: state.functions.get(key)!,
        }),
      ])
    ),
}))
vi.mock("#/components/PageContextChat", () => ({ useChatPageContext: vi.fn() }))
vi.mock("#/components/Reports", () => ({
  ReportErrorScreen: () => null,
  ReportList: ({ viewpoints }: { viewpoints: { points: string[] }[] }) => (
    <div>{viewpoints.flatMap(v => v.points).join(",")}</div>
  ),
  AnalystViewpointsLoading: () => <div role="status">Viewpoints loading</div>,
}))
vi.mock("#/components/DailyNews", () => ({
  DailyNewsLoading: () => <div role="status">News loading</div>,
  DailyNews: ({ news }: { news: { edition_id: string } | null }) => (
    <div>{news?.edition_id ?? "News unavailable"}</div>
  ),
}))
const { Route } = await import("./index")
let client: QueryClient
function show() {
  const ReportsPage = Route.options.component
  if (!ReportsPage) throw new Error("missing component")
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={createI18n("en")}>
        <ReportsPage />
      </I18nextProvider>
    </QueryClientProvider>
  )
}
beforeEach(() => {
  state.hydrated = true
  client = new QueryClient()
  state.functions.clear()
  for (const key of ["markets", "reports", "viewpoints"])
    state.functions.set(key, vi.fn().mockResolvedValue([]))
  state.functions.set(
    "news",
    vi.fn().mockResolvedValue({ edition_id: "news-ready" })
  )
})
afterEach(() => {
  cleanup()
  client.clear()
})
describe("reports browser sections", () => {
  it("has no market loader/preload and SSR hydration gate sends zero requests", () => {
    expect(Route.options.loader).toBeUndefined()
    state.hydrated = false
    show()
    expect(screen.getByText("News loading")).toBeVisible()
    expect(screen.getByText("Viewpoints loading")).toBeVisible()
    for (const fn of state.functions.values()) expect(fn).not.toHaveBeenCalled()
  })
  it("renders viewpoints while news and report summaries are still pending", async () => {
    state.functions.set(
      "news",
      vi.fn(() => new Promise(() => {}))
    )
    state.functions.set(
      "reports",
      vi.fn(() => new Promise(() => {}))
    )
    state.functions.set(
      "viewpoints",
      vi.fn().mockResolvedValue([{ points: ["Independent viewpoint"] }])
    )
    show()
    expect(await screen.findByText("Independent viewpoint")).toBeVisible()
    expect(screen.getByText("News loading")).toBeVisible()
  })
  it("offers local retry after first error", async () => {
    state.functions.set(
      "news",
      vi
        .fn()
        .mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValueOnce({ edition_id: "recovered" })
    )
    show()
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }))
    expect(await screen.findByText("recovered")).toBeVisible()
  })
  it("keeps content during stale background refresh and failure", async () => {
    client.setQueryData(
      ["market", "u", "o", "news"],
      { edition_id: "cached news" },
      { updatedAt: Date.now() - 60_001 }
    )
    let reject!: (error: Error) => void
    state.functions.set(
      "news",
      vi.fn(
        () =>
          new Promise((_resolve, fail) => {
            reject = fail
          })
      )
    )
    show()
    expect(screen.getByText("cached news")).toBeVisible()
    await waitFor(() => expect(state.functions.get("news")).toHaveBeenCalled())
    reject(new Error("offline"))
    expect(await screen.findByRole("button", { name: "Retry" })).toBeVisible()
    expect(screen.getByText("cached news")).toBeVisible()
  })
})
