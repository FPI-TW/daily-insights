import { createFileRoute, redirect } from "@tanstack/react-router"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { type AdminConversation, getAdminConversation } from "#/lib/admin-chat"
import { useTranslation } from "react-i18next"

export const Route = createFileRoute(
  "/{-$locale}/admin/_authenticated/conversations/$conversationId"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin")
      throw redirect({
        to: "/{-$locale}/admin/audio",
        params: { locale: context.locale },
      })
  },
  loader: ({ params }) =>
    getAdminConversation({ data: { id: params.conversationId } }),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: ConversationPage,
})

function ConversationPage() {
  const conversation = Route.useLoaderData()
  return <ConversationDetail conversation={conversation} />
}

export function ConversationDetail({
  conversation,
}: {
  conversation: AdminConversation
}) {
  const { t } = useTranslation()
  return (
    <main className="page-shell" aria-labelledby="conversation-title">
      <h1
        className="text-2xl font-extrabold text-sea-ink"
        id="conversation-title"
      >
        {t("conversationTitle")}
      </h1>
      <div className="space-y-3">
        {conversation.messages.map(message => (
          <article
            className="rounded-lg border border-line bg-surface p-4"
            key={message.id}
          >
            <p className="m-0 text-xs font-bold text-sea-ink-soft uppercase">
              {message.role} · {message.status}
            </p>
            <p className="mb-0 whitespace-pre-wrap text-sm text-sea-ink">
              {message.content}
            </p>
          </article>
        ))}
      </div>
      <section className="mt-6" aria-labelledby="generation-facts-title">
        <h2
          className="text-lg font-extrabold text-sea-ink"
          id="generation-facts-title"
        >
          {t("generationFacts")}
        </h2>
        <div className="space-y-3">
          {conversation.generations.map(generation => (
            <article
              className="rounded-lg border border-line bg-surface p-4"
              key={generation.id}
            >
              <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationProvider")}
                  </dt>
                  <dd className="m-0 text-sea-ink">{generation.provider}</dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationModel")}
                  </dt>
                  <dd className="m-0 text-sea-ink">
                    {generation.requested_model} /{" "}
                    {generation.resolved_model ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationPromptVersion")}
                  </dt>
                  <dd className="m-0 text-sea-ink">
                    {generation.prompt_version}
                  </dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationStatus")}
                  </dt>
                  <dd className="m-0 text-sea-ink">{generation.status}</dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationContext")}
                  </dt>
                  <dd className="m-0 break-all text-sea-ink">
                    {generation.context_digest ?? "—"} (
                    {generation.context_truncated
                      ? t("generationTruncated")
                      : t("generationCompleteContext")}
                    )
                  </dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationUsage")}
                  </dt>
                  <dd className="m-0 text-sea-ink">
                    {generation.input_tokens ?? "—"} /{" "}
                    {generation.output_tokens ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationLatency")}
                  </dt>
                  <dd className="m-0 text-sea-ink">
                    {generation.latency_ms ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="font-bold text-sea-ink-soft">
                    {t("generationErrorCode")}
                  </dt>
                  <dd className="m-0 text-sea-ink">
                    {generation.error_code ?? "—"}
                  </dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      </section>
    </main>
  )
}
