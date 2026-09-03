import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Locale } from "@daily-insights/api-client"
import { createI18n } from "#/lib/i18n"
import { PageContextChatProvider, useChatPageContext } from "./PageContextChat"

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

function renderGlobalChat() {
  const i18n = createI18n("en")
  return i18n.changeLanguage("en").then(() =>
    render(
      <I18nextProvider i18n={i18n}>
        <PageContextChatProvider locale="en" enabled>
          <main>Customer account page</main>
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

function pendingStreamResponse() {
  let streamController: ReadableStreamDefaultController<Uint8Array> | null =
    null
  return {
    response: new Response(
      new ReadableStream({
        start(controller) {
          streamController = controller
        },
      })
    ),
    finish(parts: string[]) {
      for (const part of parts) streamController?.enqueue(encoder.encode(part))
      streamController?.close()
    },
  }
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
      screen.queryByRole("button", { name: "AI Q&A" })
    ).not.toBeInTheDocument()
  })

  it("uses global market context on customer pages without page-specific context", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
    )
    await renderGlobalChat()

    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "What changed?" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )

    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const request = JSON.parse(
      String(vi.mocked(fetch).mock.calls[0]?.[1]?.body)
    )
    expect(request.page_context).toEqual({ kind: "global" })
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
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
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
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
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
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
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
    const launcher = screen.getByRole("button", { name: "AI Q&A" })
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
      expect(screen.getByRole("button", { name: "AI Q&A" })).toHaveFocus()
    )
  })

  it("renders GFM tables in an accessible horizontal scroll container", async () => {
    const markdown = [
      "## Market comparison",
      "",
      "| Market | Move | View |",
      "| --- | ---: | --- |",
      "| US equities | **+1.2%** | Risk-on |",
      "| Crypto | -0.8% | `Volatile` |",
    ].join("\n")
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse([
        `event: delta\ndata: ${JSON.stringify({ text: markdown })}\n\n`,
        'event: done\ndata: {"status":"complete"}\n\n',
      ])
    )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "Compare markets" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )

    const table = await screen.findByRole("table")
    expect(table).toHaveTextContent("US equities")
    expect(screen.getByRole("columnheader", { name: "Market" })).toBeVisible()
    expect(screen.getByText("+1.2%").tagName).toBe("STRONG")
    expect(screen.getByText("Volatile").tagName).toBe("CODE")
    const scrollContainer = screen.getByRole("region", {
      name: "Scrollable response table",
    })
    expect(scrollContainer).toHaveClass("overflow-x-auto")
    expect(scrollContainer).toHaveAttribute("tabindex", "0")
  })

  it("offers exactly two selected-text actions and sends AI Insights immediately", async () => {
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
    const menuItems = screen.getAllByRole("menuitem")
    expect(menuItems).toHaveLength(2)
    for (const menuItem of menuItems) {
      expect(menuItem).toHaveClass(
        "focus:!outline-none",
        "focus-visible:!outline-none",
        "focus-visible:!outline-offset-0"
      )
    }
    expect(screen.getByRole("menuitem", { name: "AI Insights" })).toBeVisible()
    expect(
      screen.getByRole("menuitem", { name: "AI Discussion" })
    ).toBeVisible()

    fireEvent.click(screen.getByRole("menuitem", { name: "AI Insights" }))

    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0]?.[1]?.body))
    expect(body.message).toBe("Selectable market context")
    expect(screen.queryByText("Quoted selection")).not.toBeInTheDocument()
  })

  it("shows a non-retryable error for an over-limit AI Insight after a request", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
    )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "An earlier request" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    await screen.findByText("Answer status: complete")

    const reportText = document.createElement("p")
    reportText.textContent = "x".repeat(4001)
    document.querySelector("main")?.append(reportText)
    const range = document.createRange()
    range.selectNodeContents(reportText)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)

    fireEvent.contextMenu(reportText, { clientX: 40, clientY: 60 })
    fireEvent.click(screen.getByRole("menuitem", { name: "AI Insights" }))

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The selected text is too long to send."
    )
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
    expect(fetch).toHaveBeenCalledOnce()
  })

  it("preserves Retry for a pending request when an over-limit selection is attempted", async () => {
    const pendingResponse = pendingStreamResponse()
    vi.mocked(fetch)
      .mockResolvedValueOnce(pendingResponse.response)
      .mockResolvedValueOnce(
        streamResponse(['event: done\ndata: {"status":"complete"}\n\n'])
      )
    await renderChat()
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
    fireEvent.change(screen.getByLabelText("Enter your question"), {
      target: { value: "Retry the original request" },
    })
    fireEvent.submit(
      screen.getByRole("button", { name: "Send question" }).closest("form")!
    )
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce())

    const reportText = document.createElement("p")
    reportText.textContent = "x".repeat(4001)
    document.querySelector("main")?.append(reportText)
    const range = document.createRange()
    range.selectNodeContents(reportText)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    fireEvent.contextMenu(reportText, { clientX: 40, clientY: 60 })

    expect(screen.queryByRole("menu")).toBeNull()
    expect(fetch).toHaveBeenCalledOnce()

    pendingResponse.finish([
      'event: error\ndata: {"code":"provider_error","partial":false}\n\n',
    ])
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    const first = JSON.parse(String(vi.mocked(fetch).mock.calls[0]?.[1]?.body))
    const second = JSON.parse(String(vi.mocked(fetch).mock.calls[1]?.[1]?.body))
    expect(second.message).toBe("Retry the original request")
    expect(second.client_request_id).toBe(first.client_request_id)
  })

  it("keeps the two-row selection menu inside the viewport at the bottom edge", async () => {
    await renderChat()
    const reportText = screen.getByText("Selectable market context")
    const range = document.createRange()
    range.selectNodeContents(reportText)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)

    fireEvent.contextMenu(reportText, {
      clientX: 40,
      clientY: window.innerHeight,
    })

    expect(screen.getByRole("menu")).toHaveStyle({
      top: `${window.innerHeight - 96}px`,
    })
  })

  it.each<[string, Locale, string, string]>([
    ["zh-Hant", "zh-hant", "AI 洞察", "AI 申論"],
    ["zh-Hans", "zh-hans", "AI 洞察", "AI 申论"],
  ])(
    "renders %s selected-text action labels",
    async (_, locale, insight, discussion) => {
      const i18n = createI18n(locale)
      await i18n.changeLanguage(locale)
      render(
        <I18nextProvider i18n={i18n}>
          <PageContextChatProvider locale={locale} enabled>
            <ContextFixture />
          </PageContextChatProvider>
        </I18nextProvider>
      )
      const reportText = screen.getByText("Selectable market context")
      const range = document.createRange()
      range.selectNodeContents(reportText)
      const selection = window.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(range)

      fireEvent.contextMenu(reportText, { clientX: 40, clientY: 60 })

      expect(screen.getByRole("menuitem", { name: insight })).toBeVisible()
      expect(screen.getByRole("menuitem", { name: discussion })).toBeVisible()
    }
  )

  it("attaches selected report text for AI Discussion until the user submits", async () => {
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
    fireEvent.click(screen.getByRole("menuitem", { name: "AI Discussion" }))

    expect(screen.getByText("Quoted selection")).toBeInTheDocument()
    expect(screen.getByLabelText("Enter your question")).toHaveFocus()
    expect(fetch).not.toHaveBeenCalled()
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
    const launcher = screen.getByRole("button", { name: "AI Q&A" })
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
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
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
    fireEvent.click(screen.getByRole("button", { name: "AI Q&A" }))
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
