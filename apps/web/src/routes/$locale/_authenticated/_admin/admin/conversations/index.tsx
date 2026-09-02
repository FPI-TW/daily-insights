import {
  createFileRoute,
  Link,
  redirect,
  useNavigate,
} from "@tanstack/react-router"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import {
  adminConversationSearchSchema,
  getAdminConversations,
} from "#/lib/admin-chat"
import { useTranslation } from "react-i18next"

export const Route = createFileRoute(
  "/$locale/_authenticated/_admin/admin/conversations/"
)({
  beforeLoad: ({ context }) => {
    if (context.user.system_role !== "admin")
      throw redirect({
        to: "/$locale/admin/audio",
        params: { locale: context.locale },
      })
  },
  validateSearch: adminConversationSearchSchema,
  loaderDeps: ({ search }) => search,
  loader: ({ deps }) => getAdminConversations({ data: deps }),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: ConversationsPage,
})

function ConversationsPage() {
  const { items } = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { t } = useTranslation()
  const next = Route.useLoaderData().next_cursor
  const filters = [
    "organization_id",
    "member_id",
    "created_after",
    "created_before",
  ] as const
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const values = new FormData(event.currentTarget)
    void navigate({
      search: {
        organization_id:
          String(values.get("organization_id") || "") || undefined,
        member_id: String(values.get("member_id") || "") || undefined,
        created_after: String(values.get("created_after") || "") || undefined,
        created_before: String(values.get("created_before") || "") || undefined,
        history: [],
      },
    })
  }
  function page(cursor: string | undefined, history: string[]) {
    void navigate({ search: { ...search, cursor, history } })
  }
  return (
    <main className="page-shell" aria-labelledby="conversations-title">
      <h1
        className="text-2xl font-extrabold text-sea-ink"
        id="conversations-title"
      >
        {t("conversationsTitle")}
      </h1>
      <form
        className="mb-4 grid gap-3 rounded-lg border border-line bg-surface p-4 md:grid-cols-4"
        onSubmit={submit}
      >
        {filters.slice(0, 2).map(name => (
          <label className="text-xs font-bold text-sea-ink-soft" key={name}>
            {t(
              name === "organization_id"
                ? "conversationOrganizationId"
                : "conversationMemberId"
            )}
            <input
              className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-sea-ink"
              defaultValue={search[name] ?? ""}
              name={name}
              pattern="[0-9a-fA-F-]{36}"
            />
          </label>
        ))}
        {filters.slice(2).map(name => (
          <label className="text-xs font-bold text-sea-ink-soft" key={name}>
            {t(
              name === "created_after" ? "conversationFrom" : "conversationTo"
            )}
            <input
              className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-sea-ink"
              defaultValue={search[name] ?? ""}
              name={name}
              type="date"
            />
          </label>
        ))}
        <button
          className="min-h-9 rounded-md bg-lagoon px-3 text-sm font-bold text-white md:col-span-4"
          type="submit"
        >
          {t("conversationApplyFilters")}
        </button>
      </form>
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-line text-sea-ink-soft">
            <tr>
              <th className="p-3">{t("conversationMember")}</th>
              <th className="p-3">{t("conversationMessages")}</th>
              <th className="p-3">{t("conversationCreated")}</th>
            </tr>
          </thead>
          <tbody>
            {items.map(item => (
              <tr className="border-b border-line last:border-0" key={item.id}>
                <td className="p-3">
                  <Link
                    className="font-bold text-lagoon"
                    to="/$locale/admin/conversations/$conversationId"
                    params={{ locale, conversationId: item.id }}
                  >
                    {item.member_email}
                  </Link>
                </td>
                <td className="p-3">{item.message_count}</td>
                <td className="p-3">
                  {new Intl.DateTimeFormat(locale).format(
                    new Date(item.created_at)
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <nav className="mt-4 flex gap-2" aria-label={t("conversationPagination")}>
        <button
          className="min-h-9 rounded-md border border-line px-3 text-sm font-bold text-sea-ink disabled:opacity-50"
          disabled={!search.history.length}
          type="button"
          onClick={() => {
            const previous = search.history.at(-1)
            page(previous, search.history.slice(0, -1))
          }}
        >
          {t("conversationPrevious")}
        </button>
        <button
          className="min-h-9 rounded-md border border-line px-3 text-sm font-bold text-sea-ink disabled:opacity-50"
          disabled={!next}
          type="button"
          onClick={() =>
            page(
              next ?? undefined,
              [...search.history, search.cursor].filter(
                (value): value is string => Boolean(value)
              )
            )
          }
        >
          {t("conversationNext")}
        </button>
      </nav>
    </main>
  )
}
