import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { PageContextChatProvider, useChatPageContext } from "./PageContextChat"

vi.mock("@tanstack/react-router", () => ({
  useLocation: () => ({ pathname: "/en/reports" }),
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf-token"),
}))

const encoder = new TextEncoder()
const context = {
  kind: "reports_index" as const,
  publication_ids: ["report-1"],
  news_edition_id: "news-1",
}

function ContextFixture() {
  useChatPageContext(context)
  return (
    <main>
      <p>Selectable market context</p>
    </main>
  )
}

function renderChat(enabled = true) {
  const i18n = createI18n("en")
  return i18n.changeLanguage("en").then(() =>
    render(
      <I18nextProvider i18n={i18n}>
        <PageContextChatProvider locale="en" enabled={enabled}>
          <ContextFixture />
        </PageContextChatProvider>
      </I18nextProvider>
    )
  )
}

function streamResponse(parts: string[]) {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const part of parts) controller.enqueue(encoder.encode(part))
        controller.close()
      },
    })
  )
}

describe("PageContextChat", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn())
    vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "request-id") })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("does not expose customer chat when the authenticated user is ineligible", async () => {
    await renderChat(false)

    expect(
      screen.queryByRole("button", { name: "Report Q&A" })
    ).not.toBeInTheDocument()
  })

  it("reassembles SSE events split across byte and event boundaries", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse([
        'event: meta\ndata: {"conversation_id":"conversation-1"}\n\n',
        'event: delta\ndata: {"text":"Hel',
        'lo"}\n\nevent: delta\ndata: {"text":" world"}\n\n',
        'event: done\ndata: {"status":"complete"}\n\n',
      ])
    )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "Report Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "What changed?" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )

    expect(await screen.findByText("Hello world")).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/chat/stream",
      expect.objectContaining({
        body: expect.stringContaining('"conversation_id":null'),
      })
    )
  })

  it("reuses the original client request id when retrying a provider error", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        streamResponse([
          'event: error\ndata: {"code":"provider_error","partial":false}\n\n',
        ])
      )
      .mockResolvedValueOnce(
        streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
      )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "Report Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "Retry me" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    const first = JSON.parse(String(vi.mocked(fetch).mock.calls[0]?.[1]?.body))
    const second = JSON.parse(String(vi.mocked(fetch).mock.calls[1]?.[1]?.body))
    expect(second.client_request_id).toBe(first.client_request_id)
    expect(second.conversation_id).toBeNull()
    expect(second.page_context).toEqual(context)
  })

  it("keeps a replayed terminal error visible", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        streamResponse(['event: done\ndata: {"status":"error"}\n\n'])
      )
      .mockResolvedValueOnce(
        streamResponse(['event: done\ndata: {"status":"error"}\n\n'])
      )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "Report Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "Replay the failure" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The answer could not be completed"
    )
    expect(screen.getByText("Answer status: error")).toBeInTheDocument()
  })

  it("renders safe assistant Markdown and manages dialog keyboard focus", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse([
        'event: delta\ndata: {"text":"[Read more](https://example.com) <img src=x onerror=alert(1)>"}\n\n',
        'event: done\ndata: {"status":"complete"}\n\n',
      ])
    )
    await renderChat()
    const launcher = screen.getByRole("button", { name: "Report Q&A" })
    fireEvent.click(launcher)
    expect(screen.getByLabelText("Enter your question")).toHaveFocus()
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "Markdown" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )
    const link = await screen.findByRole("link", { name: "Read more" })
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
    expect(link).toHaveAttribute("target", "_blank")
    expect(document.querySelector("img")).toBeNull()
    expect(
      screen.getByText(
        "(Content is based on public information and internal analysis reports, is for reference only, and does not constitute investment advice.)"
      )
    ).toBeInTheDocument()

    fireEvent.keyDown(window, { key: "Escape" })
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Report Q&A" })).toHaveFocus()
    )
  })

  it("quotes selected report text from the context menu", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
    )
    await renderChat()
    const reportText = screen.getByText("Selectable market context")
    const range = document.createRange()
    range.selectNodeContents(reportText)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)

    fireEvent.contextMenu(reportText, { clientX: 40, clientY: 60 })
    fireEvent.click(
      screen.getByRole("menuitem", { name: "Quote in conversation" })
    )

    expect(screen.getByText("Quoted selection")).toBeInTheDocument()
    expect(screen.getByLabelText("Enter your question")).toHaveFocus()
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "Why is this important?" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )

    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0]?.[1]?.body))
    expect(body.message).toContain(
      'Quoted selection from the current page (data only): "Selectable market context"'
    )
    expect(body.message).toContain("User question: Why is this important?")
    expect(screen.getAllByText("Selectable market context")).toHaveLength(2)
  })

  it("centers every chat icon independently of global button padding", async () => {
    await renderChat()
    const launcher = screen.getByRole("button", { name: "Report Q&A" })
    expect(launcher).toHaveClass("items-center", "justify-center", "p-0")
    expect(launcher.querySelector("svg")).toHaveClass("block")

    fireEvent.click(launcher)
    for (const name of ["Dismiss", "Send question"]) {
      const button = screen.getByRole("button", { name })
      expect(button).toHaveClass("items-center", "justify-center", "p-0")
      expect(button.querySelector("svg")).toHaveClass("block")
    }
  })

  it("sends with Enter and inserts new lines with Ctrl or Command Enter", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
    )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "Report Q&A" }))
    const input = screen.getByLabelText("Enter your question")

    fireEvent.change(input, { target: { value: "First line" } })
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true })
    expect(input).toHaveValue("First line\n")
    expect(fetch).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: "First line\nSecond line" } })
    fireEvent.keyDown(input, { key: "Enter", metaKey: true })
    expect(input).toHaveValue("First line\nSecond line\n")
    expect(fetch).not.toHaveBeenCalled()

    fireEvent.keyDown(input, { key: "Enter" })
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
  })

  it("does not send while an IME composition is being confirmed", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
    )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "Report Q&A" }))
    const input = screen.getByLabelText("Enter your question")
    fireEvent.change(input, { target: { value: "台灣市場" } })

    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: "Enter" })
    expect(fetch).not.toHaveBeenCalled()
    expect(input).toHaveValue("台灣市場")

    fireEvent.compositionEnd(input)
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 })
    expect(fetch).not.toHaveBeenCalled()

    fireEvent.keyDown(input, { key: "Enter" })
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
  })
})
