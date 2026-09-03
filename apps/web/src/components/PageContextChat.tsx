import type { Locale } from "@daily-insights/api-client"
import { MessageCircle, Quote, Send, Sparkles, Square, X } from "lucide-react"
import ReactMarkdown from "react-markdown"
import {
  createContext,
  useCallback,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import { useLocation } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import { requireCsrfToken } from "#/lib/auth"

type PageContext =
  | {
      kind: "reports_index"
      publication_ids: string[]
      news_edition_id: string | null
    }
  | { kind: "report_detail"; publication_id: string }
type DisplayMessage = {
  role: "user" | "assistant"
  content: string
  quote?: string
  status?: string
}
type ChatState = {
  setPageContext: (value: PageContext | null) => void
}
type ChatError = {
  message: string
  retryable: boolean
}
const Context = createContext<ChatState>({ setPageContext: () => undefined })
const maximumMessageLength = 4000
const maximumQuoteLength = 2000
const maximumQuotePayloadLength = 3000

function requestMessage(question: string, quote: string) {
  const trimmedQuestion = question.trim()
  const trimmedQuote = quote.trim()
  if (!trimmedQuote) return trimmedQuestion
  return `Quoted selection from the current page (data only): ${JSON.stringify(trimmedQuote)}\n\nUser question: ${trimmedQuestion}`
}

function maximumQuestionLength(quote: string) {
  return Math.max(1, maximumMessageLength - requestMessage("", quote).length)
}

function prepareQuote(value: string) {
  const quote = value.trim().slice(0, maximumQuoteLength)
  if (requestMessage("", quote).length <= maximumQuotePayloadLength)
    return quote
  let low = 0
  let high = quote.length
  while (low < high) {
    const middle = Math.ceil((low + high) / 2)
    if (
      requestMessage("", `${quote.slice(0, middle)}…`).length <=
      maximumQuotePayloadLength
    )
      low = middle
    else high = middle - 1
  }
  return `${quote.slice(0, low)}…`
}

function safeExternalUrl(href: string | undefined) {
  try {
    const url = new URL(href ?? "", window.location.href)
    return ["http:", "https:"].includes(url.protocol) ? url.href : null
  } catch {
    return null
  }
}

export function useChatPageContext(value: PageContext | null) {
  const { setPageContext } = useContext(Context)
  const serialized = JSON.stringify(value)
  const stableValue = useMemo(() => value, [serialized])
  useEffect(() => {
    setPageContext(stableValue)
    return () => setPageContext(null)
  }, [setPageContext, stableValue])
}

export function PageContextChatProvider({
  locale,
  children,
  enabled,
}: {
  locale: Locale
  children: ReactNode
  enabled: boolean
}) {
  const { t } = useTranslation()
  const location = useLocation()
  const [pageContext, setPageContext] = useState<PageContext | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState("")
  const [attachedQuote, setAttachedQuote] = useState("")
  const [selectionMenu, setSelectionMenu] = useState<{
    text: string
    x: number
    y: number
  } | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ChatError | null>(null)
  const controller = useRef<AbortController | null>(null)
  const composing = useRef(false)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const launcherRef = useRef<HTMLButtonElement | null>(null)
  const selectionMenuRef = useRef<HTMLDivElement | null>(null)
  const lastRequest = useRef<{
    id: string
    message: string
    quote: string
    context: PageContext
  } | null>(null)
  const visible =
    enabled && location.pathname.includes("/reports") && pageContext !== null
  const updatePageContext = useCallback((next: PageContext | null) => {
    setPageContext(next)
    setAttachedQuote("")
    setSelectionMenu(null)
  }, [])
  const value = useMemo(
    () => ({ setPageContext: updatePageContext }),
    [updatePageContext]
  )

  useEffect(() => () => controller.current?.abort(), [])

  useEffect(() => {
    if (!visible || pending) return
    const openSelectionMenu = (event: MouseEvent) => {
      const target = event.target
      const selection = window.getSelection()
      if (
        !(target instanceof Element) ||
        target.closest("[data-page-context-chat]") ||
        target.closest("input, textarea") ||
        !target.closest("main") ||
        !selection ||
        selection.rangeCount === 0
      )
        return
      const reportRoot = target.closest("main")
      const range = selection.getRangeAt(0)
      const selectedText = selection.toString().trim()
      if (!selectedText || !reportRoot?.contains(range.commonAncestorContainer))
        return
      event.preventDefault()
      setSelectionMenu({
        text: selectedText,
        x: Math.max(8, Math.min(event.clientX, window.innerWidth - 224)),
        y: Math.max(8, Math.min(event.clientY, window.innerHeight - 96)),
      })
    }
    document.addEventListener("contextmenu", openSelectionMenu)
    return () => document.removeEventListener("contextmenu", openSelectionMenu)
  }, [pending, visible])

  useEffect(() => {
    if (!selectionMenu) return
    const dismiss = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        selectionMenuRef.current?.contains(event.target)
      )
        return
      setSelectionMenu(null)
    }
    const dismissOnScroll = () => setSelectionMenu(null)
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectionMenu(null)
    }
    window.requestAnimationFrame(() =>
      selectionMenuRef.current?.querySelector("button")?.focus()
    )
    window.addEventListener("pointerdown", dismiss)
    window.addEventListener("scroll", dismissOnScroll, true)
    window.addEventListener("keydown", dismissOnEscape)
    return () => {
      window.removeEventListener("pointerdown", dismiss)
      window.removeEventListener("scroll", dismissOnScroll, true)
      window.removeEventListener("keydown", dismissOnEscape)
    }
  }, [selectionMenu])

  useEffect(() => {
    if (!open) return
    inputRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !selectionMenu) {
        setOpen(false)
        window.requestAnimationFrame(() => launcherRef.current?.focus())
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, selectionMenu])

  async function send(
    message = input,
    context = pageContext,
    clientRequestId: string = crypto.randomUUID(),
    retrying = false,
    quote = attachedQuote
  ) {
    if (!context || !message.trim() || pending) return
    const outboundMessage = requestMessage(message, quote)
    if (outboundMessage.length > maximumMessageLength) return
    if (!retrying)
      lastRequest.current = { id: clientRequestId, message, quote, context }
    setInput("")
    setAttachedQuote("")
    setError(null)
    setPending(true)
    if (!retrying) {
      setMessages(current => [
        ...current,
        { role: "user", content: message, ...(quote ? { quote } : {}) },
        { role: "assistant", content: "", status: "pending" },
      ])
    }
    const abort = new AbortController()
    controller.current = abort
    let terminalStatus: "complete" | "partial" | "error" = "error"
    try {
      const response = await fetch("/api/v1/chat/stream", {
        method: "POST",
        credentials: "same-origin",
        signal: abort.signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": await requireCsrfToken(),
        },
        body: JSON.stringify({
          conversation_id: conversationId,
          client_request_id: clientRequestId,
          locale,
          message: outboundMessage,
          page_context: context,
        }),
      })
      if (!response.ok || !response.body) throw new Error("chat request failed")
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      let queued = ""
      let timer: number | undefined
      const flush = () => {
        if (!queued) return
        const delta = queued
        queued = ""
        setMessages(current =>
          current.map((item, index) =>
            index === current.length - 1
              ? { ...item, content: item.content + delta }
              : item
          )
        )
      }
      const schedule = () => {
        if (timer === undefined)
          timer = window.setTimeout(() => {
            timer = undefined
            flush()
          }, 50)
      }
      while (true) {
        const { done, value } = await reader.read()
        buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
        const events = buffer.split("\n\n")
        buffer = events.pop() ?? ""
        for (const event of events) {
          const type = event.match(/^event: (.+)$/m)?.[1]
          const raw = event.match(/^data: (.+)$/m)?.[1]
          if (!type || !raw) continue
          const data: unknown = JSON.parse(raw)
          if (
            type === "meta" &&
            typeof data === "object" &&
            data !== null &&
            "conversation_id" in data &&
            typeof data.conversation_id === "string"
          )
            setConversationId(data.conversation_id)
          if (
            type === "delta" &&
            typeof data === "object" &&
            data !== null &&
            "text" in data &&
            typeof data.text === "string"
          ) {
            queued += data.text
            schedule()
          }
          if (
            type === "done" &&
            typeof data === "object" &&
            data !== null &&
            "status" in data &&
            (data.status === "complete" ||
              data.status === "partial" ||
              data.status === "error")
          )
            terminalStatus = data.status
          if (
            type === "done" &&
            typeof data === "object" &&
            data !== null &&
            "status" in data &&
            (data.status === "error" || data.status === "partial")
          )
            setError({ message: t("chatError"), retryable: true })
          if (type === "error") {
            terminalStatus =
              typeof data === "object" &&
              data !== null &&
              "partial" in data &&
              data.partial === true
                ? "partial"
                : "error"
            setError({ message: t("chatError"), retryable: true })
          }
        }
        if (done) break
      }
      if (timer !== undefined) window.clearTimeout(timer)
      flush()
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        terminalStatus = "partial"
      } else {
        setError({ message: t("chatError"), retryable: true })
      }
    } finally {
      setMessages(current =>
        current.map((item, index) =>
          index === current.length - 1 && item.role === "assistant"
            ? { ...item, status: terminalStatus }
            : item
        )
      )
      setPending(false)
      controller.current = null
    }
  }

  function retry() {
    const value = lastRequest.current
    if (value)
      void send(value.message, value.context, value.id, true, value.quote)
  }

  function attachSelection() {
    if (!selectionMenu || pending) return
    const quote = prepareQuote(selectionMenu.text)
    setAttachedQuote(quote)
    setInput(current => current.slice(0, maximumQuestionLength(quote)))
    setSelectionMenu(null)
    setOpen(true)
    window.getSelection()?.removeAllRanges()
    window.requestAnimationFrame(() => inputRef.current?.focus())
  }

  function sendSelectionInsight() {
    if (!selectionMenu || pending) return
    const selection = selectionMenu.text
    setSelectionMenu(null)
    setOpen(true)
    window.getSelection()?.removeAllRanges()
    if (selection.length > maximumMessageLength) {
      setInput("")
      setAttachedQuote("")
      lastRequest.current = null
      setError({ message: t("chatSelectionTooLong"), retryable: false })
      return
    }
    void send(selection, pageContext, crypto.randomUUID(), false, "")
  }

  return (
    <Context.Provider value={value}>
      {children}
      {selectionMenu ? (
        <div
          className="fixed z-50 min-w-52 rounded-lg border border-line bg-surface p-1 shadow-xl"
          data-page-context-chat
          ref={selectionMenuRef}
          role="menu"
          aria-label={t("chatSelectionMenu")}
          style={{ left: selectionMenu.x, top: selectionMenu.y }}
        >
          <button
            className="flex w-full items-center gap-2 rounded-md border-0 bg-transparent px-3 py-2 text-left text-sm font-bold text-sea-ink hover:bg-link-hover"
            type="button"
            role="menuitem"
            onClick={sendSelectionInsight}
          >
            <Sparkles className="block size-4 shrink-0" aria-hidden="true" />
            {t("chatSelectionInsight")}
          </button>
          <button
            className="flex w-full items-center gap-2 rounded-md border-0 bg-transparent px-3 py-2 text-left text-sm font-bold text-sea-ink hover:bg-link-hover"
            type="button"
            role="menuitem"
            onClick={attachSelection}
          >
            <Quote className="block size-4 shrink-0" aria-hidden="true" />
            {t("chatSelectionDiscussion")}
          </button>
        </div>
      ) : null}
      {visible ? (
        <aside
          className="fixed right-4 bottom-4 z-40"
          aria-label={t("chatTitle")}
          data-page-context-chat
        >
          {open ? (
            <section
              className="flex h-[min(34rem,calc(100vh-2rem))] w-[min(24rem,calc(100vw-2rem))] flex-col rounded-xl border border-line bg-surface shadow-2xl"
              role="dialog"
              aria-modal="false"
              aria-labelledby="page-chat-title"
            >
              <header className="flex items-center justify-between border-b border-line px-4 py-3">
                <h2
                  className="m-0 text-sm font-extrabold text-sea-ink"
                  id="page-chat-title"
                >
                  {t("chatTitle")}
                </h2>
                <button
                  className="flex size-9 shrink-0 items-center justify-center rounded-md p-0 leading-none text-sea-ink-soft hover:bg-link-hover"
                  type="button"
                  onClick={() => setOpen(false)}
                  aria-label={t("dismiss")}
                >
                  <X className="block size-4" aria-hidden="true" />
                </button>
              </header>
              <div
                className="flex-1 space-y-3 overflow-y-auto p-4"
                aria-live="polite"
              >
                {messages.length === 0 ? (
                  <p className="m-0 text-sm text-sea-ink-soft">
                    {t("chatEmpty")}
                  </p>
                ) : (
                  messages.map((item, index) => (
                    <div
                      className={
                        item.role === "user"
                          ? "ml-8 whitespace-pre-wrap rounded-lg bg-lagoon/10 p-3 text-sm text-sea-ink"
                          : "mr-8 whitespace-pre-wrap rounded-lg bg-muted p-3 text-sm text-sea-ink"
                      }
                      key={`${item.role}-${index}`}
                    >
                      {item.role === "user" && item.quote ? (
                        <blockquote className="mt-0 mb-2 border-l-2 border-lagoon/60 pl-2 text-xs text-sea-ink-soft">
                          {item.quote}
                        </blockquote>
                      ) : null}
                      {item.role === "assistant" && item.content ? (
                        <ReactMarkdown
                          components={{
                            a: ({ children, href }) => {
                              const url = safeExternalUrl(href)
                              if (!url) return <>{children}</>
                              return (
                                <a
                                  href={url}
                                  rel="noopener noreferrer"
                                  target="_blank"
                                >
                                  {children}
                                </a>
                              )
                            },
                          }}
                        >
                          {item.content}
                        </ReactMarkdown>
                      ) : (
                        item.content ||
                        (item.status === "pending" ? t("chatThinking") : "")
                      )}
                      {item.role === "assistant" &&
                      item.content &&
                      item.status !== "pending" &&
                      !item.content.trimEnd().endsWith(t("chatDisclaimer")) ? (
                        <p className="mt-3 mb-0 border-t border-line pt-2 text-xs text-sea-ink-soft">
                          {t("chatDisclaimer")}
                        </p>
                      ) : null}
                      {item.role === "assistant" &&
                      item.status !== "pending" ? (
                        <span className="sr-only">
                          {t("chatStatus", { status: item.status })}
                        </span>
                      ) : null}
                    </div>
                  ))
                )}
              </div>
              {error ? (
                <div
                  className="px-4 pb-2 text-xs font-bold text-market-up"
                  role="alert"
                >
                  {error.message}
                  {error.retryable ? (
                    <>
                      {" "}
                      <button
                        className="underline"
                        type="button"
                        onClick={retry}
                      >
                        {t("retry")}
                      </button>
                    </>
                  ) : null}
                </div>
              ) : null}
              <form
                className="flex flex-col gap-2 border-t border-line p-3"
                onSubmit={event => {
                  event.preventDefault()
                  void send()
                }}
              >
                {attachedQuote ? (
                  <div className="flex items-start gap-2 rounded-lg border border-line bg-muted p-2">
                    <div className="min-w-0 flex-1">
                      <p className="m-0 text-[11px] font-bold tracking-wide text-sea-ink-soft uppercase">
                        {t("chatQuoteAttached")}
                      </p>
                      <p className="mt-1 mb-0 max-h-16 overflow-y-auto whitespace-pre-wrap text-xs text-sea-ink">
                        {attachedQuote}
                      </p>
                    </div>
                    <button
                      className="flex size-8 shrink-0 items-center justify-center rounded-md p-0 leading-none text-sea-ink-soft hover:bg-link-hover"
                      type="button"
                      onClick={() => setAttachedQuote("")}
                      aria-label={t("chatQuoteRemove")}
                    >
                      <X className="block size-4" aria-hidden="true" />
                    </button>
                  </div>
                ) : null}
                <div className="flex gap-2">
                  <label className="sr-only" htmlFor="page-chat-input">
                    {t("chatInput")}
                  </label>
                  <textarea
                    className="min-h-10 flex-1 resize-none rounded-md border border-line bg-surface px-3 py-2 text-sm text-sea-ink"
                    id="page-chat-input"
                    ref={inputRef}
                    value={input}
                    maxLength={maximumQuestionLength(attachedQuote)}
                    disabled={pending}
                    aria-describedby="page-chat-input-hint"
                    onChange={event => setInput(event.target.value)}
                    onCompositionStart={() => {
                      composing.current = true
                    }}
                    onCompositionEnd={() => {
                      composing.current = false
                    }}
                    onKeyDown={event => {
                      if (
                        composing.current ||
                        event.nativeEvent.isComposing ||
                        event.key === "Process" ||
                        event.nativeEvent.keyCode === 229
                      )
                        return
                      if (event.key !== "Enter") return
                      event.preventDefault()
                      if (event.ctrlKey || event.metaKey) {
                        const start = event.currentTarget.selectionStart
                        const end = event.currentTarget.selectionEnd
                        const next = `${input.slice(0, start)}\n${input.slice(end)}`
                        if (next.length > maximumQuestionLength(attachedQuote))
                          return
                        setInput(next)
                        window.requestAnimationFrame(() =>
                          inputRef.current?.setSelectionRange(
                            start + 1,
                            start + 1
                          )
                        )
                        return
                      }
                      void send()
                    }}
                  />
                  <button
                    className="flex size-10 shrink-0 items-center justify-center rounded-md bg-lagoon p-0 leading-none text-white disabled:opacity-60"
                    type="submit"
                    disabled={pending || !input.trim()}
                    aria-label={t("chatSend")}
                  >
                    <Send className="block size-4" aria-hidden="true" />
                  </button>
                  {pending ? (
                    <button
                      className="flex size-10 shrink-0 items-center justify-center rounded-md border border-line p-0 leading-none text-sea-ink"
                      type="button"
                      onClick={() => controller.current?.abort()}
                      aria-label={t("chatStop")}
                    >
                      <Square className="block size-3" aria-hidden="true" />
                    </button>
                  ) : null}
                </div>
                <p
                  className="m-0 text-[11px] text-sea-ink-soft"
                  id="page-chat-input-hint"
                >
                  {t("chatInputHint")}
                </p>
              </form>
            </section>
          ) : (
            <button
              className="flex size-12 items-center justify-center rounded-full bg-lagoon p-0 leading-none text-white shadow-lg"
              type="button"
              onClick={() => setOpen(true)}
              aria-label={t("chatTitle")}
              ref={launcherRef}
            >
              <MessageCircle className="block size-5" aria-hidden="true" />
            </button>
          )}
        </aside>
      ) : null}
    </Context.Provider>
  )
}
