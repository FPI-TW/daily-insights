import type { Locale } from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table"
import { type ReactNode, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { ResponsiveTable } from "#/components/ResponsiveTable"
import { formatTimestamp } from "#/lib/format"
import {
  browserNewsroomAdminClient,
  buildSourcesReviewMarkdown,
  downloadTextFile,
  newsroomAdminKeys,
  newsroomMarkets,
  type NewsroomMarket,
  type NewsroomSource,
  type NewsroomSourceHealth,
  sourceChanges,
  sourceCreateInput,
  sourceMatchesMarkets,
  type SourceFormValues,
  sourcesReviewFilename,
} from "#/lib/newsroom-admin"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { ConfirmDialog } from "./ConfirmDialog"
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

function SortHeader({
  label,
  sorted,
  onToggle,
}: {
  label: string
  sorted: false | "asc" | "desc"
  onToggle: () => void
}) {
  const marker = sorted === "asc" ? " ↑" : sorted === "desc" ? " ↓" : ""
  return (
    <button
      type="button"
      className="inline-flex items-center gap-0.5 border-0 bg-transparent p-0 text-left text-xs font-extrabold text-sea-ink-soft"
      onClick={onToggle}
    >
      {label}
      <span aria-hidden="true">{marker || " ↕"}</span>
    </button>
  )
}

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
  const [deleting, setDeleting] = useState<NewsroomSource | null>(null)
  const [marketFilter, setMarketFilter] = useState<NewsroomMarket[]>([])
  const [sorting, setSorting] = useState<SortingState>([])
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
  const remove = useNewsroomAction({
    locale,
    run: (client, source: NewsroomSource, csrf) =>
      client.deleteSource(source.id, csrf),
    invalidates: () => [newsroomAdminKeys.sources()],
    onSuccess: () => setDeleting(null),
  })

  const filteredSources = useMemo(() => {
    const rows = sources.data?.sources ?? []
    return rows.filter(source => sourceMatchesMarkets(source, marketFilter))
  }, [marketFilter, sources.data?.sources])

  // Narrow screens show each cell with its column name (ResponsiveTable).
  const columnLabels: Record<string, string> = {
    name: t("newsroomAdminSourceName"),
    kind: t("newsroomAdminSourceKind"),
    markets: t("newsroomAdminSourceMarkets"),
    trust_tier: t("newsroomAdminSourceTrust"),
    weight: t("newsroomAdminSourceWeight"),
    poll_interval_minutes: t("newsroomAdminSourceInterval"),
    health: t("newsroomAdminSourceHealth"),
    articles_today: t("newsroomAdminSourceArticlesToday"),
    articles_7d: t("newsroomAdminSourceArticles"),
    actions: t("newsroomAdminSourceActions"),
  }
  const columns = useMemo(
    () => [
      column.accessor("name", {
        header: () => t("newsroomAdminSourceName"),
        cell: info => (
          <span className="grid justify-items-start gap-1">
            <span className="flex flex-wrap items-center gap-2">
              <span className="text-base font-extrabold tracking-tight">
                {info.getValue()}
              </span>
              {info.row.original.kind === "manual" ? (
                <StatusBadge tone="neutral">
                  {t("newsroomAdminSourceSystem")}
                </StatusBadge>
              ) : null}
              {!info.row.original.enabled &&
              info.row.original.kind !== "manual" ? (
                <StatusBadge tone="neutral">
                  {t("newsroomAdminSourceDisable")}
                </StatusBadge>
              ) : null}
            </span>
            <span className="font-mono text-xs text-sea-ink-soft">
              {info.row.original.key}
            </span>
            <span className="text-xs text-sea-ink-soft">
              {info.row.original.hostname}
            </span>
          </span>
        ),
      }),
      column.accessor("kind", {
        header: () => t("newsroomAdminSourceKind"),
        enableSorting: false,
        cell: info => (
          <span className="inline-flex rounded-md border border-line bg-chip px-2 py-0.5 font-mono text-xs whitespace-nowrap">
            {info.getValue()}
          </span>
        ),
      }),
      column.accessor("markets", {
        header: () => t("newsroomAdminSourceMarkets"),
        enableSorting: false,
        cell: info => (
          <span className="flex flex-wrap gap-1">
            {info.getValue().length === 0
              ? "—"
              : info.getValue().map(market => (
                  <span
                    key={market}
                    className="rounded-full border border-line px-2 py-0.5 text-xs"
                  >
                    {t(`newsroomAdminMarket_${market}`)}
                  </span>
                ))}
          </span>
        ),
      }),
      column.accessor("trust_tier", {
        header: ({ column: col }) => (
          <SortHeader
            label={t("newsroomAdminSourceTrust")}
            sorted={col.getIsSorted()}
            onToggle={() => col.toggleSorting()}
          />
        ),
        sortingFn: "basic",
      }),
      column.accessor("weight", {
        header: ({ column: col }) => (
          <SortHeader
            label={t("newsroomAdminSourceWeightShort")}
            sorted={col.getIsSorted()}
            onToggle={() => col.toggleSorting()}
          />
        ),
        cell: info => <span className="tabular-nums">×{info.getValue()}</span>,
        sortingFn: "basic",
      }),
      column.accessor("poll_interval_minutes", {
        header: ({ column: col }) => (
          <SortHeader
            label={t("newsroomAdminSourceIntervalShort")}
            sorted={col.getIsSorted()}
            onToggle={() => col.toggleSorting()}
          />
        ),
        cell: info =>
          t("newsroomAdminSourceIntervalValue", { minutes: info.getValue() }),
        sortingFn: "basic",
      }),
      column.accessor("health", {
        header: () => t("newsroomAdminSourceHealth"),
        enableSorting: false,
        cell: info =>
          info.row.original.kind === "manual" ? (
            <span className="text-xs text-sea-ink-soft">
              {t("newsroomAdminSourceManualNote")}
            </span>
          ) : (
            <SourceHealth source={info.row.original} />
          ),
      }),
      column.accessor("articles_today", {
        header: ({ column: col }) => (
          <SortHeader
            label={t("newsroomAdminSourceArticlesToday")}
            sorted={col.getIsSorted()}
            onToggle={() => col.toggleSorting()}
          />
        ),
        cell: info => (
          <span className="text-lg font-extrabold tabular-nums">
            {info.getValue()}
          </span>
        ),
        sortingFn: "basic",
      }),
      column.accessor("articles_7d", {
        header: ({ column: col }) => (
          <SortHeader
            label={t("newsroomAdminSourceArticles")}
            sorted={col.getIsSorted()}
            onToggle={() => col.toggleSorting()}
          />
        ),
        cell: info => (
          <span className="text-lg font-extrabold tabular-nums">
            {info.getValue()}
          </span>
        ),
        sortingFn: "basic",
      }),
      column.display({
        id: "actions",
        enableSorting: false,
        header: () => t("newsroomAdminSourceActions"),
        cell: info => {
          const source = info.row.original
          return (
            <span className="flex flex-wrap gap-1.5">
              <button
                type="button"
                className="rounded-lg border border-line bg-transparent px-2.5 py-1 text-xs font-bold"
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
              {/* The manual source is never polled and cannot be switched off or deleted. */}
              {source.kind === "manual" ? null : (
                <>
                  <button
                    type="button"
                    className="rounded-lg border border-line bg-transparent px-2.5 py-1 text-xs font-bold"
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
                  <button
                    type="button"
                    className="rounded-lg border border-destructive/40 bg-transparent px-2.5 py-1 text-xs font-bold text-destructive"
                    aria-label={t("newsroomAdminSourceDeleteLabel", {
                      name: source.name,
                    })}
                    onClick={() => {
                      remove.reset()
                      setDeleting(source)
                    }}
                  >
                    {t("newsroomAdminSourceDelete")}
                  </button>
                </>
              )}
            </span>
          )
        },
      }),
    ],
    [remove, save, t, toggle]
  )
  const table = useReactTable({
    data: filteredSources,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getRowId: source => source.id,
  })

  function toggleMarket(market: NewsroomMarket) {
    setMarketFilter(current =>
      current.includes(market)
        ? current.filter(item => item !== market)
        : [...current, market]
    )
  }

  function exportSources() {
    const rows = filteredSources
    downloadTextFile(sourcesReviewFilename(), buildSourcesReviewMarkdown(rows))
  }

  const allSources = sources.data?.sources ?? []
  const enabledCount = allSources.filter(
    source => source.enabled && source.kind !== "manual"
  ).length
  const disabledCount = allSources.filter(
    source => !source.enabled && source.kind !== "manual"
  ).length
  const unhealthyCount = allSources.filter(
    source => source.health === "unhealthy" || source.health === "degraded"
  ).length
  const articlesToday = allSources.reduce(
    (total, source) => total + source.articles_today,
    0
  )
  const articles7d = allSources.reduce(
    (total, source) => total + source.articles_7d,
    0
  )

  return (
    <main className="page-shell">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-6">
        <div className="max-w-2xl">
          <p className="eyebrow">{t("adminPortal")}</p>
          <h1 className="mt-2 text-[clamp(2rem,4vw,2.75rem)] leading-none font-extrabold tracking-[-0.05em]">
            {t("newsroomAdminSourcesTitle")}
          </h1>
          <p className="mt-3 mb-0 max-w-xl leading-7 text-sea-ink-soft">
            {t("newsroomAdminSourcesDescription")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {headerAction}
          <button
            type="button"
            className="rounded-full border border-line bg-transparent px-4 py-2 text-sm font-bold"
            disabled={!sources.data || filteredSources.length === 0}
            onClick={exportSources}
          >
            {t("newsroomAdminSourceExport")}
          </button>
          <button
            type="button"
            className="primary-action rounded-full px-4 py-2"
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

      {sources.data ? (
        <section
          aria-label={t("newsroomAdminSourcesTitle")}
          className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-5"
        >
          <SourceStat
            label={t("newsroomAdminSourceStatEnabled")}
            value={enabledCount}
          />
          <SourceStat
            label={t("newsroomAdminSourceStatDisabled")}
            value={disabledCount}
          />
          <SourceStat
            label={t("newsroomAdminSourceStatUnhealthy")}
            value={unhealthyCount}
            emphasis={unhealthyCount > 0 ? "danger" : "neutral"}
          />
          <SourceStat
            label={t("newsroomAdminSourceStatToday")}
            value={articlesToday}
          />
          <SourceStat
            label={t("newsroomAdminSourceStatArticles")}
            value={articles7d}
          />
        </section>
      ) : null}

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
        <section
          className="surface-panel grid gap-5 p-4 sm:p-5"
          aria-busy={sources.isFetching}
        >
          <div className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-4">
            <fieldset className="m-0 grid gap-2 border-0 p-0">
              <legend className="mb-1 text-xs font-extrabold tracking-[0.08em] text-sea-ink-soft uppercase">
                {t("newsroomAdminSourceMarketFilter")}
              </legend>
              <div className="flex flex-wrap gap-2">
                {newsroomMarkets.map(market => {
                  const selected = marketFilter.includes(market)
                  return (
                    <label
                      key={market}
                      className={
                        selected
                          ? "inline-flex cursor-pointer items-center rounded-full border border-lagoon bg-lagoon-tint px-3 py-1.5 text-sm font-bold text-lagoon-deep"
                          : "inline-flex cursor-pointer items-center rounded-full border border-line bg-transparent px-3 py-1.5 text-sm font-bold text-sea-ink-soft"
                      }
                    >
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={selected}
                        onChange={() => toggleMarket(market)}
                      />
                      {t(`newsroomAdminMarket_${market}`)}
                    </label>
                  )
                })}
              </div>
            </fieldset>
            <div className="flex flex-wrap items-center gap-3">
              <p className="m-0 text-sm text-sea-ink-soft">
                {t("newsroomAdminSourceFilterCount", {
                  shown: filteredSources.length,
                  total: sources.data.sources.length,
                })}
              </p>
              {marketFilter.length > 0 ? (
                <button
                  type="button"
                  className="rounded-full border border-line bg-transparent px-3 py-1.5 text-xs font-bold"
                  onClick={() => setMarketFilter([])}
                >
                  {t("newsroomAdminSourceMarketFilterClear")}
                </button>
              ) : null}
            </div>
          </div>

          {toggle.error ? (
            <p role="alert" className="m-0 font-bold text-destructive">
              {toggle.error}
            </p>
          ) : null}

          {filteredSources.length === 0 ? (
            <p className="m-0 p-6 text-center text-sea-ink-soft">
              {t("newsroomAdminSourceFilterEmpty")}
            </p>
          ) : (
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
                  <tr
                    key={row.id}
                    className={
                      row.original.enabled || row.original.kind === "manual"
                        ? "border-t border-line align-top transition-colors hover:bg-link-hover/40"
                        : "border-t border-line align-top opacity-70 transition-colors hover:bg-link-hover/40"
                    }
                  >
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
          )}
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

      {deleting ? (
        <ConfirmDialog
          title={t("newsroomAdminSourceDeleteTitle", { name: deleting.name })}
          message={t("newsroomAdminSourceDeleteMessage", { key: deleting.key })}
          confirmLabel={t("newsroomAdminSourceDeleteConfirm")}
          pending={remove.isPending}
          error={remove.error}
          onClose={() => setDeleting(null)}
          onConfirm={() => void remove.run(deleting)}
        />
      ) : null}
    </main>
  )
}

function SourceStat({
  label,
  value,
  emphasis = "neutral",
}: {
  label: string
  value: number
  emphasis?: "neutral" | "danger"
}) {
  return (
    <div className="surface-panel grid gap-1 px-4 py-3">
      <span className="text-xs font-bold tracking-wide text-sea-ink-soft uppercase">
        {label}
      </span>
      <span
        className={
          emphasis === "danger"
            ? "text-3xl font-extrabold tracking-tight text-destructive tabular-nums"
            : "text-3xl font-extrabold tracking-tight tabular-nums"
        }
      >
        {value}
      </span>
    </div>
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
