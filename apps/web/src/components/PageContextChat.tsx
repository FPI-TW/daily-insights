import type { Locale } from "@daily-insights/api-client"
import { MessageCircle, Send, Square, X } from "lucide-react"
import ReactMarkdown from "react-markdown"
import {
  createContext,
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
  status?: string
}
type ChatState = {
  setPageContext: (value: PageContext | null) => void
}
const Context = createContext<ChatState>({ setPageContext: () => undefined })

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
}: {
  locale: Locale
  children: ReactNode
}) {
  const { t } = useTranslation()
  const location = useLocation()
  const [pageContext, setPageContext] = useState<PageContext | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const controller = useRef<AbortController | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const launcherRef = useRef<HTMLButtonElement | null>(null)
  const lastRequest = useRef<{
    id: string
    message: string
    context: PageContext
  } | null>(null)
  const visible = location.pathname.includes("/reports") && pageContext !== null
  const value = useMemo(() => ({ setPageContext }), [])

  useEffect(() => () => controller.current?.abort(), [])

  useEffect(() => {
    if (!open) return
    inputRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false)
        window.requestAnimationFrame(() => launcherRef.current?.focus())
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open])

  async function send(
    message = input,
    context = pageContext,
    clientRequestId: string = crypto.randomUUID(),
    retrying = false
  ) {
    if (!context || !message.trim() || pending) return
    if (!retrying)
      lastRequest.current = { id: clientRequestId, message, context }
    setInput("")
    setError("")
    setPending(true)
    if (!retrying) {
      setMessages(current => [
        ...current,
        { role: "user", content: message },
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
          message,
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
            setError(t("chatError"))
          if (type === "error") {
            terminalStatus =
              typeof data === "object" &&
              data !== null &&
              "partial" in data &&
              data.partial === true
                ? "partial"
                : "error"
            setError(t("chatError"))
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
        setError(t("chatError"))
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
    if (value) void send(value.message, value.context, value.id, true)
  }

  return (
    <Context.Provider value={value}>
      {children}
      {visible ? (
        <aside
          className="fixed right-4 bottom-4 z-40"
          aria-label={t("chatTitle")}
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
                  className="grid size-9 place-items-center rounded-md text-sea-ink-soft hover:bg-link-hover"
                  type="button"
                  onClick={() => setOpen(false)}
                  aria-label={t("dismiss")}
                >
                  <X className="size-4" />
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
                  {error}{" "}
                  <button className="underline" type="button" onClick={retry}>
                    {t("retry")}
                  </button>
                </div>
              ) : null}
              <form
                className="flex gap-2 border-t border-line p-3"
                onSubmit={event => {
                  event.preventDefault()
                  void send()
                }}
              >
                <label className="sr-only" htmlFor="page-chat-input">
                  {t("chatInput")}
                </label>
                <textarea
                  className="min-h-10 flex-1 resize-none rounded-md border border-line bg-surface px-3 py-2 text-sm text-sea-ink"
                  id="page-chat-input"
                  ref={inputRef}
                  value={input}
                  maxLength={4000}
                  disabled={pending}
                  onChange={event => setInput(event.target.value)}
                />
                <button
                  className="grid size-10 place-items-center rounded-md bg-lagoon text-white disabled:opacity-60"
                  type="submit"
                  disabled={pending || !input.trim()}
                  aria-label={t("chatSend")}
                >
                  <Send className="size-4" />
                </button>
                {pending ? (
                  <button
                    className="grid size-10 place-items-center rounded-md border border-line text-sea-ink"
                    type="button"
                    onClick={() => controller.current?.abort()}
                    aria-label={t("chatStop")}
                  >
                    <Square className="size-3" />
                  </button>
                ) : null}
              </form>
            </section>
          ) : (
            <button
              className="grid size-12 place-items-center rounded-full bg-lagoon text-white shadow-lg"
              type="button"
              onClick={() => setOpen(true)}
              aria-label={t("chatTitle")}
              ref={launcherRef}
            >
              <MessageCircle className="size-5" />
            </button>
          )}
        </aside>
      ) : null}
    </Context.Provider>
  )
}
