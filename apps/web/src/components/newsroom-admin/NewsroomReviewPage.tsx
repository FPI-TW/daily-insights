import type { Locale } from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import { LoaderCircle } from "lucide-react"
import { type ReactNode, useEffect, useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import {
  browserNewsroomAdminClient,
  formatTaipeiTime,
  hasPendingWork,
  minutesUntil,
  moveItem,
  newsroomAdminKeys,
  newsroomMarkets,
  type NewsroomEdition,
  type NewsroomEditionDetail,
  type NewsroomMarket,
} from "#/lib/newsroom-admin"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { CandidateList } from "./CandidateList"
import { ManualUrlForm } from "./ManualUrlForm"
import { ReviewItemCard } from "./ReviewItemCard"
import { StatusBadge } from "./StatusBadge"
import { useNewsroomAction } from "./useNewsroomAction"

// Items still waiting on analysis are refreshed this often so the review
// sees them turn ready without a manual reload.
const PENDING_REFRESH_MS = 30_000

type Approval = { kind: "market"; edition: NewsroomEdition } | { kind: "all" }

/** The daily review console at /admin/newsroom (spec §6.4, D1, D19). */
export function NewsroomReviewPage({
  locale,
  date,
  market,
  onDateChange,
  onMarketChange,
  headerAction,
}: {
  locale: Locale
  date: string
  market: NewsroomMarket
  onDateChange: (date: string) => void
  onMarketChange: (market: NewsroomMarket) => void
  headerAction?: ReactNode
}) {
  const { t } = useTranslation()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const now = useNow()
  const [approval, setApproval] = useState<Approval | null>(null)
  const day = useQuery({
    queryKey: newsroomAdminKeys.day(date),
    queryFn: () => browserNewsroomAdminClient().editionDay(date),
  })
  const detail = useQuery({
    queryKey: newsroomAdminKeys.edition(date, market),
    queryFn: () => browserNewsroomAdminClient().editionDetail(date, market),
    refetchInterval: query =>
      hasPendingWork(query.state.data) ? PENDING_REFRESH_MS : false,
  })
  useEffect(() => {
    if (day.error) void redirectExpired(day.error)
  }, [day.error, redirectExpired])
  useEffect(() => {
    if (detail.error) void redirectExpired(detail.error)
  }, [detail.error, redirectExpired])

  const publish = useNewsroomAction({
    locale,
    run: (client, target: Approval, csrf) =>
      target.kind === "all"
        ? client.publishDay(date, csrf)
        : client.publishEdition(target.edition.id, csrf),
    invalidates: target =>
      target.kind === "all"
        ? [newsroomAdminKeys.day(date), newsroomAdminKeys.editionsOfDate(date)]
        : [
            newsroomAdminKeys.day(date),
            newsroomAdminKeys.edition(date, target.edition.market_code),
          ],
    onSuccess: () => setApproval(null),
  })

  if (day.isPending || detail.isPending) {
    return (
      <main className="page-shell" role="status" aria-live="polite">
        <span className="sr-only">{t("newsroomAdminLoading")}</span>
        <div className="h-10 w-72 animate-pulse rounded bg-link-hover" />
        <div className="mt-6 h-12 max-w-xl animate-pulse rounded-xl bg-link-hover" />
        <div className="mt-6 grid gap-3">
          <div className="h-40 animate-pulse rounded-xl bg-link-hover" />
          <div className="h-40 animate-pulse rounded-xl bg-link-hover" />
        </div>
      </main>
    )
  }

  if (!day.data || !detail.data) {
    return (
      <main className="page-shell">
        <div
          role="alert"
          className="surface-panel flex flex-wrap items-center gap-3 p-5"
        >
          <span className="font-bold text-destructive">
            {t("newsroomAdminLoadFailed")}
          </span>
          <button
            type="button"
            onClick={() => {
              void day.refetch()
              void detail.refetch()
            }}
          >
            {t("newsroomAdminRetry")}
          </button>
        </div>
      </main>
    )
  }

  const summary = day.data
  const drafts = summary.markets.filter(
    entry => entry.edition?.status === "draft"
  )
  const refreshing = day.isFetching || detail.isFetching

  return (
    <main className="page-shell">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-3xl">
          <p className="eyebrow">{t("adminPortal")}</p>
          <h1 className="mt-2 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
            {t("newsroomAdminTitle")}
          </h1>
          <p className="mt-3 mb-0 leading-7 text-sea-ink-soft">
            {t("newsroomAdminDescription")}
          </p>
        </div>
        {headerAction}
      </header>

      <section
        className="surface-panel mb-6 flex flex-wrap items-end gap-4 p-4"
        aria-label={t("newsroomAdminDayControls")}
      >
        <label className="w-44">
          {t("newsroomAdminEditionDate")}
          <input
            type="date"
            value={date}
            required
            onChange={event => {
              if (event.target.value) onDateChange(event.target.value)
            }}
          />
        </label>
        <div className="grid justify-items-start gap-1 text-sm">
          {summary.is_today ? (
            <StatusBadge tone="positive">{t("newsroomAdminToday")}</StatusBadge>
          ) : (
            <StatusBadge tone="caution">
              {t("newsroomAdminNotToday")}
            </StatusBadge>
          )}
          <span className="text-sea-ink-soft">
            {t("newsroomAdminUntriaged", {
              count: summary.untriaged_articles,
            })}
            {summary.triage_failed_articles > 0
              ? ` · ${t("newsroomAdminTriageFailed", {
                  count: summary.triage_failed_articles,
                })}`
              : ""}
          </span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          {refreshing ? (
            <span
              role="status"
              aria-live="polite"
              className="inline-flex items-center gap-1 text-sm text-sea-ink-soft"
            >
              <LoaderCircle
                className="size-4 animate-spin"
                aria-hidden="true"
              />
              {t("newsroomAdminRefreshing")}
            </span>
          ) : null}
          <button
            type="button"
            className="primary-action"
            disabled={drafts.length === 0 || publish.isPending}
            onClick={() => {
              publish.reset()
              setApproval({ kind: "all" })
            }}
          >
            {t("newsroomAdminApproveAll", { count: drafts.length })}
          </button>
        </div>
      </section>

      <div
        role="tablist"
        aria-label={t("newsroomAdminMarkets")}
        className="mb-4 flex gap-2 overflow-x-auto"
      >
        {newsroomMarkets.map(code => {
          const entry = summary.markets.find(
            candidate => candidate.market_code === code
          )
          const selected = code === market
          return (
            <button
              key={code}
              type="button"
              role="tab"
              id={`newsroom-tab-${code}`}
              aria-selected={selected}
              aria-controls="newsroom-market-panel"
              className={`flex shrink-0 items-center gap-2 ${selected ? "border-lagoon-deep bg-lagoon-deep text-white" : ""}`}
              onClick={() => onMarketChange(code)}
            >
              <span className="font-extrabold">
                {t(`newsroomAdminMarket_${code}`)}
              </span>
              <EditionStatus edition={entry?.edition ?? null} />
              <span className="text-xs">
                {t("newsroomAdminItemCount", {
                  count: entry?.counts.active ?? 0,
                })}
              </span>
            </button>
          )
        })}
      </div>

      <MarketPanel
        locale={locale}
        date={date}
        market={market}
        detail={detail.data}
        now={now}
        onApprove={edition => {
          publish.reset()
          setApproval({ kind: "market", edition })
        }}
      />

      <section
        className="surface-panel mt-8 p-5"
        aria-labelledby="newsroom-manual-url-title"
      >
        <h2
          id="newsroom-manual-url-title"
          className="m-0 text-lg font-extrabold"
        >
          {t("newsroomAdminManualUrlTitle")}
        </h2>
        <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
          {t("newsroomAdminManualUrlDescription", { date })}
        </p>
        <ManualUrlForm locale={locale} date={date} />
      </section>

      {approval ? (
        <ApproveDialog
          approval={approval}
          drafts={drafts.length}
          pending={publish.isPending}
          error={publish.error}
          onClose={() => setApproval(null)}
          onConfirm={() => void publish.run(approval)}
        />
      ) : null}
    </main>
  )
}

function MarketPanel({
  locale,
  date,
  market,
  detail,
  now,
  onApprove,
}: {
  locale: Locale
  date: string
  market: NewsroomMarket
  detail: NewsroomEditionDetail
  now: number
  onApprove: (edition: NewsroomEdition) => void
}) {
  const { t } = useTranslation()
  const { edition, items, candidates } = detail
  const keys = [
    newsroomAdminKeys.edition(date, market),
    newsroomAdminKeys.day(date),
  ]
  const reorder = useNewsroomAction({
    locale,
    run: (client, input: { editionId: string; ids: string[] }, csrf) =>
      client.reorder(input.editionId, input.ids, csrf),
    invalidates: () => keys,
  })
  const add = useNewsroomAction({
    locale,
    run: (client, input: { editionId: string; eventId: string }, csrf) =>
      client.addItem(input.editionId, input.eventId, csrf),
    invalidates: () => keys,
  })
  const [adding, setAdding] = useState<string | null>(null)
  const mergeOptions = [
    ...items.map(item => item.event),
    ...candidates.map(candidate => candidate.event),
  ]

  return (
    <section
      id="newsroom-market-panel"
      role="tabpanel"
      aria-labelledby={`newsroom-tab-${market}`}
      className="grid gap-6"
    >
      {edition ? (
        <EditionHeader edition={edition} now={now} onApprove={onApprove} />
      ) : (
        <p className="surface-panel m-0 p-5 text-sea-ink-soft">
          {t("newsroomAdminNotAssembled")}
        </p>
      )}

      {edition ? (
        <section aria-labelledby="newsroom-items-title" className="grid gap-3">
          <h2 id="newsroom-items-title" className="m-0 text-lg font-extrabold">
            {t("newsroomAdminItems")}
          </h2>
          {reorder.error ? (
            <p role="alert" className="m-0 font-bold text-destructive">
              {reorder.error}
            </p>
          ) : null}
          {items.length === 0 ? (
            <p className="m-0 rounded-xl border border-dashed border-line p-6 text-center text-sea-ink-soft">
              {t("newsroomAdminNoItems")}
            </p>
          ) : (
            items.map((item, index) => (
              <ReviewItemCard
                key={item.id}
                locale={locale}
                date={date}
                market={market}
                edition={edition}
                item={item}
                position={index + 1}
                count={items.length}
                reorderPending={reorder.isPending}
                mergeOptions={mergeOptions}
                onMove={direction => {
                  const ids = moveItem(items, item.id, direction)
                  if (ids) void reorder.run({ editionId: edition.id, ids })
                }}
              />
            ))
          )}
        </section>
      ) : null}

      <section
        aria-labelledby="newsroom-candidates-title"
        className="grid gap-3"
      >
        <div>
          <h2
            id="newsroom-candidates-title"
            className="m-0 text-lg font-extrabold"
          >
            {t("newsroomAdminCandidates")}
          </h2>
          <p className="mt-1 mb-0 text-sm text-sea-ink-soft">
            {t("newsroomAdminCandidatesDescription")}
          </p>
        </div>
        {add.error ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {add.error}
          </p>
        ) : null}
        <CandidateList
          candidates={candidates}
          canAdd={edition !== null}
          pendingEventId={adding}
          onAdd={eventId => {
            if (!edition) return
            setAdding(eventId)
            void add
              .run({ editionId: edition.id, eventId })
              .finally(() => setAdding(null))
          }}
        />
      </section>
    </section>
  )
}

function EditionHeader({
  edition,
  now,
  onApprove,
}: {
  edition: NewsroomEdition
  now: number
  onApprove: (edition: NewsroomEdition) => void
}) {
  const { t } = useTranslation()
  const draft = edition.status === "draft"
  const minutes = minutesUntil(edition.auto_publish_at, now)
  return (
    <div className="surface-panel grid gap-3 p-5">
      <div className="flex flex-wrap items-center gap-3">
        <EditionStatus edition={edition} />
        <span className="text-sm text-sea-ink-soft">
          {draft
            ? minutes === null
              ? t("newsroomAdminAutoPublishDue", {
                  time: formatTaipeiTime(edition.auto_publish_at),
                })
              : t("newsroomAdminAutoPublishIn", {
                  time: formatTaipeiTime(edition.auto_publish_at),
                  count: minutes,
                })
            : edition.published_by_user_id
              ? t("newsroomAdminPublishedByEditor", {
                  time: formatTaipeiTime(edition.published_at ?? ""),
                })
              : t("newsroomAdminPublishedAutomatically", {
                  time: formatTaipeiTime(edition.published_at ?? ""),
                })}
        </span>
        {draft ? (
          <button
            type="button"
            className="primary-action ml-auto"
            onClick={() => onApprove(edition)}
          >
            {t("newsroomAdminApproveMarket")}
          </button>
        ) : null}
      </div>
      {edition.selection_mode === "fallback" ? (
        <p
          role="alert"
          className="m-0 rounded-lg border border-market-caution/50 bg-market-caution/10 p-3 text-sm font-bold text-market-caution"
        >
          {t("newsroomAdminFallbackWarning")}
        </p>
      ) : null}
      {edition.ignored_pending_triage > 0 ? (
        <p className="m-0 text-sm text-sea-ink-soft">
          {t("newsroomAdminIgnoredPending", {
            count: edition.ignored_pending_triage,
          })}
        </p>
      ) : null}
    </div>
  )
}

function EditionStatus({ edition }: { edition: NewsroomEdition | null }) {
  const { t } = useTranslation()
  if (!edition) {
    return <StatusBadge>{t("newsroomAdminStatus_none")}</StatusBadge>
  }
  return (
    <StatusBadge tone={edition.status === "published" ? "positive" : "caution"}>
      {t(`newsroomAdminStatus_${edition.status}`)}
    </StatusBadge>
  )
}

function ApproveDialog({
  approval,
  drafts,
  pending,
  error,
  onClose,
  onConfirm,
}: {
  approval: Approval
  drafts: number
  pending: boolean
  error: string
  onClose: () => void
  onConfirm: () => void
}) {
  const { t } = useTranslation()
  const titleId = useId()
  return (
    <Dialog open onClose={onClose} labelledBy={titleId} role="alertdialog">
      <h2 id={titleId} className="m-0 text-lg font-extrabold">
        {approval.kind === "all"
          ? t("newsroomAdminApproveAllTitle", { count: drafts })
          : t("newsroomAdminApproveMarketTitle", {
              market: t(`newsroomAdminMarket_${approval.edition.market_code}`),
            })}
      </h2>
      <p className="mt-3 text-sm leading-6 text-sea-ink-soft">
        {t("newsroomAdminApproveWarning")}
      </p>
      {error ? (
        <p role="alert" className="mt-3 mb-0 font-bold text-destructive">
          {error}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end gap-3">
        <button type="button" onClick={onClose}>
          {t("dismiss")}
        </button>
        <button
          type="button"
          className="primary-action"
          disabled={pending}
          onClick={onConfirm}
        >
          {t("newsroomAdminApproveConfirm")}
        </button>
      </div>
    </Dialog>
  )
}

function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}
