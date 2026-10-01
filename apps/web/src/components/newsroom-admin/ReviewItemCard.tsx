import type { Locale } from "@daily-insights/api-client"
import { ArrowDown, ArrowUp, ChevronDown, Star } from "lucide-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  itemAlerts,
  newsroomAdminKeys,
  type NewsroomEdition,
  type NewsroomItem,
  type NewsroomMarket,
} from "#/lib/newsroom-admin"
import { EventEditDialog, TextEditDialog, WHY_MAX } from "./EditDialogs"
import { EventArticlesPanel, type MergeOption } from "./EventArticlesPanel"
import { StatusBadge } from "./StatusBadge"
import { useNewsroomAction } from "./useNewsroomAction"

type Editing = "event" | "why" | null

/** One placed event: what readers will see, its review state, and D19 actions. */
export function ReviewItemCard({
  locale,
  date,
  market,
  edition,
  item,
  position,
  count,
  reorderPending,
  onMove,
  mergeOptions,
}: {
  locale: Locale
  date: string
  market: NewsroomMarket
  edition: NewsroomEdition
  item: NewsroomItem
  position: number
  count: number
  reorderPending: boolean
  onMove: (direction: -1 | 1) => void
  mergeOptions: MergeOption[]
}) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState<Editing>(null)
  const { event } = item
  const marketKeys = [
    newsroomAdminKeys.edition(date, market),
    newsroomAdminKeys.day(date),
  ]
  const itemAction = useNewsroomAction({
    locale,
    run: (
      client,
      action: "remove" | "restore" | "hide" | "unhide",
      csrf: string
    ) => client.itemAction(item.id, action, csrf),
    invalidates: () => marketKeys,
  })
  const editEvent = useNewsroomAction({
    locale,
    run: (client, changes: Parameters<typeof client.editEvent>[1], csrf) =>
      client.editEvent(event.id, changes, csrf),
    // The headline and summary are shared by every market of the date.
    invalidates: () => [
      newsroomAdminKeys.editionsOfDate(date),
      newsroomAdminKeys.event(event.id),
    ],
    onSuccess: () => setEditing(null),
  })
  const editWhy = useNewsroomAction({
    locale,
    run: (client, why: string, csrf) => client.editWhy(item.id, why, csrf),
    invalidates: () => [newsroomAdminKeys.edition(date, market)],
    onSuccess: () => setEditing(null),
  })
  const reanalyze = useNewsroomAction({
    locale,
    run: (client, _: void, csrf) => client.reanalyze(event.id, csrf),
    invalidates: () => [
      newsroomAdminKeys.day(date),
      newsroomAdminKeys.editionsOfDate(date),
      newsroomAdminKeys.event(event.id),
    ],
  })
  const removed = item.removed_at !== null
  const hidden = item.hidden_at !== null
  const published = edition.status === "published"
  const alerts = itemAlerts(item)
  const actionError = itemAction.error || reanalyze.error
  const headline = event.headline_zh_hant ?? event.working_title
  const panelId = `newsroom-item-${item.id}-articles`

  return (
    <article
      className={`surface-panel grid gap-3 p-4 ${removed || hidden ? "opacity-60" : ""}`}
      aria-labelledby={`newsroom-item-${item.id}-title`}
    >
      <header className="flex items-start gap-3">
        <span className="grid size-8 shrink-0 place-items-center rounded-full bg-link-hover text-sm font-extrabold">
          {position}
        </span>
        <div className="grid min-w-0 flex-1 gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Stars stars={item.stars} />
            {item.origin === "manual" ? (
              <StatusBadge>{t("newsroomAdminOriginManual")}</StatusBadge>
            ) : null}
            {removed ? (
              <StatusBadge tone="caution">
                {t("newsroomAdminRemoved")}
              </StatusBadge>
            ) : null}
            {hidden ? (
              <StatusBadge tone="caution">
                {t("newsroomAdminHidden")}
              </StatusBadge>
            ) : null}
            {alerts.map(alert => (
              <StatusBadge
                key={alert}
                tone={
                  alert === "analysisPending" || alert === "whyPending"
                    ? "neutral"
                    : "danger"
                }
              >
                {t(`newsroomAdminAlert_${alert}`)}
              </StatusBadge>
            ))}
            <span className="text-xs text-sea-ink-soft">
              {t("newsroomAdminCoverage", {
                articles: event.article_count,
                sources: event.source_count,
              })}
            </span>
          </div>
          <h3
            id={`newsroom-item-${item.id}-title`}
            className={`m-0 text-lg leading-snug font-extrabold ${event.headline_zh_hant ? "" : "text-sea-ink-soft"}`}
          >
            {headline}
          </h3>
        </div>
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            className="grid size-9 place-items-center p-0"
            aria-label={t("newsroomAdminMoveUp", { title: headline })}
            disabled={position === 1 || reorderPending}
            onClick={() => onMove(-1)}
          >
            <ArrowUp className="size-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            className="grid size-9 place-items-center p-0"
            aria-label={t("newsroomAdminMoveDown", { title: headline })}
            disabled={position === count || reorderPending}
            onClick={() => onMove(1)}
          >
            <ArrowDown className="size-4" aria-hidden="true" />
          </button>
        </div>
      </header>

      <dl className="m-0 grid gap-3 text-sm leading-6">
        <div>
          <dt className="text-xs font-extrabold text-sea-ink-soft">
            {t("newsroomAdminSummary")}
          </dt>
          <dd className="m-0">
            {event.summary_zh_hant ?? (
              <span className="text-sea-ink-soft">
                {t("newsroomAdminNotWritten")}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-extrabold text-sea-ink-soft">
            {t("newsroomAdminWhy", {
              market: t(`newsroomAdminMarket_${market}`),
            })}
          </dt>
          <dd className="m-0">
            {item.why_zh_hant ?? (
              <span className="text-sea-ink-soft">
                {t("newsroomAdminNotWritten")}
              </span>
            )}
          </dd>
        </div>
        {event.related_symbols.length > 0 ? (
          <div>
            <dt className="text-xs font-extrabold text-sea-ink-soft">
              {t("newsroomAdminRelatedSymbols")}
            </dt>
            <dd className="m-0 flex flex-wrap gap-2 pt-1">
              {event.related_symbols.map(symbol => (
                <StatusBadge key={symbol.symbol}>
                  {symbol.label} · {symbol.symbol}
                </StatusBadge>
              ))}
            </dd>
          </div>
        ) : null}
        <div>
          <dt className="text-xs font-extrabold text-sea-ink-soft">
            {t("newsroomAdminSources")}
          </dt>
          <dd className="m-0">
            <ul className="m-0 grid list-none gap-1 p-0">
              {event.articles.map(article => (
                <li key={article.id} className="wrap-anywhere">
                  <span className="font-bold">{article.source_name}</span>
                  {" · "}
                  <a href={article.url} target="_blank" rel="noreferrer">
                    {article.title}
                  </a>
                </li>
              ))}
            </ul>
          </dd>
        </div>
      </dl>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={event.analysis_status !== "ready"}
          onClick={() => {
            editEvent.reset()
            setEditing("event")
          }}
        >
          {t("newsroomAdminEditEvent")}
        </button>
        <button
          type="button"
          disabled={item.why_status !== "ready"}
          onClick={() => {
            editWhy.reset()
            setEditing("why")
          }}
        >
          {t("newsroomAdminEditWhy")}
        </button>
        <button
          type="button"
          disabled={reanalyze.isPending || event.analysis_status === "pending"}
          onClick={() => void reanalyze.run()}
        >
          {t("newsroomAdminReanalyze")}
        </button>
        {published ? (
          <button
            type="button"
            disabled={itemAction.isPending}
            onClick={() => void itemAction.run(hidden ? "unhide" : "hide")}
          >
            {hidden ? t("newsroomAdminUnhide") : t("newsroomAdminHide")}
          </button>
        ) : (
          <button
            type="button"
            disabled={itemAction.isPending}
            onClick={() => void itemAction.run(removed ? "restore" : "remove")}
          >
            {removed ? t("newsroomAdminRestore") : t("newsroomAdminRemove")}
          </button>
        )}
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={panelId}
          onClick={() => setExpanded(current => !current)}
        >
          {t("newsroomAdminCompareOriginal")}
          <ChevronDown
            className={`ml-1 inline size-4 transition-transform ${expanded ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>
      </div>
      {actionError ? (
        <p role="alert" className="m-0 font-bold text-destructive">
          {actionError}
        </p>
      ) : null}
      <div id={panelId} hidden={!expanded}>
        {expanded ? (
          <EventArticlesPanel
            locale={locale}
            date={date}
            market={market}
            event={event}
            mergeOptions={mergeOptions.filter(option => option.id !== event.id)}
          />
        ) : null}
      </div>

      {editing === "event" ? (
        <EventEditDialog
          open
          headline={event.headline_zh_hant ?? ""}
          summary={event.summary_zh_hant ?? ""}
          symbols={event.related_symbols}
          pending={editEvent.isPending}
          error={editEvent.error}
          onClose={() => setEditing(null)}
          onSubmit={changes => void editEvent.run(changes)}
        />
      ) : null}
      {editing === "why" ? (
        <TextEditDialog
          open
          title={t("newsroomAdminEditWhyTitle", {
            market: t(`newsroomAdminMarket_${market}`),
          })}
          description={t("newsroomAdminEditLanguageNote")}
          label={t("newsroomAdminWhy", {
            market: t(`newsroomAdminMarket_${market}`),
          })}
          initial={item.why_zh_hant ?? ""}
          maxLength={WHY_MAX}
          pending={editWhy.isPending}
          error={editWhy.error}
          onClose={() => setEditing(null)}
          onSubmit={why => void editWhy.run(why)}
        />
      ) : null}
    </article>
  )
}

function Stars({ stars }: { stars: number | null }) {
  const { t } = useTranslation()
  if (stars === null) {
    return <StatusBadge>{t("newsroomAdminNoStars")}</StatusBadge>
  }
  return (
    <span
      className="inline-flex items-center gap-0.5 text-market-caution"
      role="img"
      aria-label={t("newsroomAdminStars", { count: stars })}
    >
      {Array.from({ length: 5 }, (_, index) => (
        <Star
          key={index}
          className={`size-4 ${index < stars ? "fill-current" : "opacity-30"}`}
          aria-hidden="true"
        />
      ))}
    </span>
  )
}
