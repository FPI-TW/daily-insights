import { render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { ConversationDetail } from "./$conversationId"

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => () => ({}),
  redirect: vi.fn(),
}))
vi.mock("#/lib/admin-chat", () => ({ getAdminConversation: vi.fn() }))

describe("ConversationDetail", () => {
  it("renders generation audit facts with semantic labels", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    render(
      <I18nextProvider i18n={i18n}>
        <ConversationDetail
          conversation={{
            usage: {
              input_tokens: 12,
              output_tokens: 34,
              total_tokens: 46,
              completeness: "complete",
            },
            id: "00000000-0000-4000-8000-000000000001",
            organization_id: "00000000-0000-4000-8000-000000000002",
            user_id: "00000000-0000-4000-8000-000000000003",
            messages: [],
            generations: [
              {
                id: "00000000-0000-4000-8000-000000000004",
                provider: "deepseek",
                requested_model: "deepseek-chat",
                resolved_model: "deepseek-v3",
                prompt_version: "chat-v1",
                context_digest: "sha256:context",
                context_truncated: true,
                input_tokens: 12,
                output_tokens: 34,
                latency_ms: 56,
                status: "partial",
                error_code: "provider_timeout",
                created_at: "2026-09-02T00:00:00Z",
              },
            ],
          }}
        />
      </I18nextProvider>
    )

    expect(
      screen.getByRole("heading", { name: "Generation facts" })
    ).toBeInTheDocument()
    expect(screen.getByText("deepseek-chat / deepseek-v3")).toBeInTheDocument()
    expect(screen.getByText("sha256:context (Truncated)")).toBeInTheDocument()
    expect(screen.getAllByText("46")).toHaveLength(2)
    expect(screen.getAllByText("Input tokens")).toHaveLength(2)
    expect(screen.getByText("provider_timeout")).toBeInTheDocument()
    expect(
      screen.getByRole("region", { name: "Token usage summary" })
    ).toBeInTheDocument()
  })
})
