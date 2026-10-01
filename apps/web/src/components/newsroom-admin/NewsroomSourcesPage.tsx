import type { Locale } from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table"
import { type ReactNode, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { ResponsiveTable } from "#/components/ResponsiveTable"
import { formatTimestamp } from "#/lib/format"
import {
  browserNewsroomAdminClient,
  newsroomAdminKeys,
  type NewsroomSource,
  type NewsroomSourceHealth,
  sourceChanges,
  sourceCreateInput,
  type SourceFormValues,
} from "#/lib/newsroom-admin"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { SourceFormDialog } from "./SourceFormDialog"
import { StatusBadge, type StatusTone } from "./StatusBadge"
import { useNewsroomAction } from "./useNewsroomAction"

const healthTones: Record<NewsroomSourceHealth, StatusTone> = {
  healthy: "positive",
  pending: "neutral",
  degraded: "caution",
  unhealthy: "danger",
  disabled: "neutral",
}

const column = createColumnHelper<NewsroomSource>()

type Editing = { source: NewsroomSource | null } | null

/** Source management at /admin/newsroom/sources (spec §4.1, D3). */
export function NewsroomSourcesPage({
  locale,
  headerAction,
}: {
  locale: Locale
  headerAction?: ReactNode
}) {
  const { t } = useTranslation()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const [editing, setEditing] = useState<Editing>(null)
  const sources = useQuery({
    queryKey: newsroomAdminKeys.sources(),
    queryFn: () => browserNewsroomAdminClient().listSources(),
  })
  useEffect(() => {
    if (sources.error) void redirectExpired(sources.error)
  }, [redirectExpired, sources.error])

  const save = useNewsroomAction({
    locale,
    run: (
      client,
      input: { source: NewsroomSource | null; values: SourceFormValues },
      csrf
    ) =>
      input.source
        ? client.updateSource(
            input.source.id,
            sourceChanges(input.source, input.values),
            csrf
          )
        : client.createSource(sourceCreateInput(input.values), csrf),
    invalidates: () => [newsroomAdminKeys.sources()],
    onSuccess: () => setEditing(null),
  })
  const toggle = useNewsroomAction({
    locale,
    run: (client, source: NewsroomSource, csrf) =>
      client.updateSource(source.id, { enabled: !source.enabled }, csrf),
    invalidates: () => [newsroomAdminKeys.sources()],
  })

  // Narrow screens show each cell with its column name (ResponsiveTable).
  const columnLabels: Record<string, string> = {
    name: t("newsroomAdminSourceName"),
    kind: t("newsroomAdminSourceKind"),
    markets: t("newsroomAdminSourceMarkets"),
    settings: t("newsroomAdminSourceSettings"),
    health: t("newsroomAdminSourceHealth"),
    articles_7d: t("newsroomAdminSourceArticles"),
    actions: t("newsroomAdminSourceActions"),
  }
  const columns = useMemo(
    () => [
      column.accessor("name", {
        header: () => t("newsroomAdminSourceName"),
        cell: info => (
          <span className="grid gap-0.5">
            <span className="font-extrabold">{info.getValue()}</span>
            <span className="text-xs text-sea-ink-soft">
              {info.row.original.key} · {info.row.original.hostname}
            </span>
          </span>
        ),
      }),
      column.accessor("kind", {
        header: () => t("newsroomAdminSourceKind"),
      }),
      column.accessor("markets", {
        header: () => t("newsroomAdminSourceMarkets"),
        cell: info =>
          info
            .getValue()
            .map(market => t(`newsroomAdminMarket_${market}`))
            .join("、") || "—",
      }),
      column.accessor(
        source =>
          `${source.trust_tier} · ×${source.weight} · ${source.poll_interval_minutes}`,
        {
          id: "settings",
          header: () => t("newsroomAdminSourceSettings"),
          cell: info =>
            t("newsroomAdminSourceSettingsValue", {
              trust: info.row.original.trust_tier,
              weight: info.row.original.weight,
              minutes: info.row.original.poll_interval_minutes,
            }),
        }
      ),
      column.accessor("health", {
        header: () => t("newsroomAdminSourceHealth"),
        cell: info => <SourceHealth source={info.row.original} />,
      }),
      column.accessor("articles_7d", {
        header: () => t("newsroomAdminSourceArticles"),
      }),
      column.display({
        id: "actions",
        header: () => t("newsroomAdminSourceActions"),
        cell: info => {
          const source = info.row.original
          return (
            <span className="flex flex-wrap gap-2">
              <button
                type="button"
                className="px-3 py-1.5 text-sm"
                aria-label={t("newsroomAdminSourceEditLabel", {
                  name: source.name,
                })}
                onClick={() => {
                  save.reset()
                  setEditing({ source })
                }}
              >
                {t("newsroomAdminSourceEdit")}
              </button>
              <button
                type="button"
                className="px-3 py-1.5 text-sm"
                disabled={toggle.isPending}
                aria-label={t(
                  source.enabled
                    ? "newsroomAdminSourceDisableLabel"
                    : "newsroomAdminSourceEnableLabel",
                  { name: source.name }
                )}
                onClick={() => void toggle.run(source)}
              >
                {source.enabled
                  ? t("newsroomAdminSourceDisable")
                  : t("newsroomAdminSourceEnable")}
              </button>
            </span>
          )
        },
      }),
    ],
    [save, t, toggle]
  )
  const table = useReactTable({
    data: sources.data?.sources ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: source => source.id,
  })

  return (
    <main className="page-shell">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-3xl">
          <p className="eyebrow">{t("adminPortal")}</p>
          <h1 className="mt-2 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
            {t("newsroomAdminSourcesTitle")}
          </h1>
          <p className="mt-3 mb-0 leading-7 text-sea-ink-soft">
            {t("newsroomAdminSourcesDescription")}
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          {headerAction}
          <button
            type="button"
            className="primary-action"
            disabled={sources.isPending}
            onClick={() => {
              save.reset()
              setEditing({ source: null })
            }}
          >
            {t("newsroomAdminSourceCreate")}
          </button>
        </div>
      </header>

      {sources.isPending ? (
        <div
          role="status"
          aria-live="polite"
          className="surface-panel grid gap-3 p-5"
        >
          <span className="sr-only">{t("newsroomAdminSourcesLoading")}</span>
          <div className="h-8 animate-pulse rounded bg-link-hover" />
          <div className="h-8 animate-pulse rounded bg-link-hover" />
          <div className="h-8 animate-pulse rounded bg-link-hover" />
        </div>
      ) : !sources.data ? (
        <div
          role="alert"
          className="surface-panel flex flex-wrap items-center gap-3 p-5"
        >
          <span className="font-bold text-destructive">
            {t("newsroomAdminSourcesLoadFailed")}
          </span>
          <button type="button" onClick={() => void sources.refetch()}>
            {t("newsroomAdminRetry")}
          </button>
        </div>
      ) : sources.data.sources.length === 0 ? (
        <p className="surface-panel m-0 p-8 text-center text-sea-ink-soft">
          {t("newsroomAdminSourcesEmpty")}
        </p>
      ) : (
        <section className="surface-panel p-4" aria-busy={sources.isFetching}>
          {toggle.error ? (
            <p role="alert" className="mt-0 font-bold text-destructive">
              {toggle.error}
            </p>
          ) : null}
          <ResponsiveTable>
            <thead>
              {table.getHeaderGroups().map(group => (
                <tr key={group.id}>
                  {group.headers.map(header => (
                    <th
                      key={header.id}
                      scope="col"
                      className="text-left text-xs font-extrabold text-sea-ink-soft"
                    >
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext()
                      )}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map(row => (
                <tr key={row.id} className="border-t border-line">
                  {row.getVisibleCells().map((cell, index) =>
                    index === 0 ? (
                      <th
                        key={cell.id}
                        scope="row"
                        className="text-left font-normal"
                      >
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext()
                        )}
                      </th>
                    ) : (
                      <td
                        key={cell.id}
                        data-label={columnLabels[cell.column.id]}
                      >
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext()
                        )}
                      </td>
                    )
                  )}
                </tr>
              ))}
            </tbody>
          </ResponsiveTable>
        </section>
      )}

      {editing ? (
        <SourceFormDialog
          source={editing.source}
          pending={save.isPending}
          error={save.error}
          onClose={() => setEditing(null)}
          onSubmit={values => save.run({ source: editing.source, values })}
        />
      ) : null}
    </main>
  )
}

function SourceHealth({ source }: { source: NewsroomSource }) {
  const { t } = useTranslation()
  return (
    <span className="grid justify-items-start gap-1 text-xs">
      <StatusBadge tone={healthTones[source.health]}>
        {t(`newsroomAdminHealth_${source.health}`)}
      </StatusBadge>
      <span className="text-sea-ink-soft">
        {source.last_success_at
          ? t("newsroomAdminSourceLastSuccess", {
              time: formatTimestamp(source.last_success_at),
            })
          : t("newsroomAdminSourceNeverSucceeded")}
      </span>
      {source.consecutive_failures > 0 ? (
        <span className="text-destructive">
          {t("newsroomAdminSourceFailures", {
            count: source.consecutive_failures,
            code: source.last_error_code ?? "—",
          })}
        </span>
      ) : null}
    </span>
  )
}
