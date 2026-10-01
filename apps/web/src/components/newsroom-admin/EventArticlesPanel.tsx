import type { Locale } from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import { formatTimestamp } from "#/lib/format"
import {
  browserNewsroomAdminClient,
  newsroomAdminKeys,
  type NewsroomArticle,
  type NewsroomEvent,
  type NewsroomMarket,
} from "#/lib/newsroom-admin"
import { BODY_MAX, TextEditDialog } from "./EditDialogs"
import { StatusBadge } from "./StatusBadge"
import { useNewsroomAction } from "./useNewsroomAction"

export type MergeOption = Pick<
  NewsroomEvent,
  "id" | "working_title" | "headline_zh_hant"
>

/**
 * The original articles behind one event, for checking the analysis against
 * the source, plus the event-level repairs: paste a body, split articles
 * out, or merge other events in (D18, D19).
 */
export function EventArticlesPanel({
  locale,
  date,
  market,
  event,
  mergeOptions,
}: {
  locale: Locale
  date: string
  market: NewsroomMarket
  event: NewsroomEvent
  mergeOptions: MergeOption[]
}) {
  const { t } = useTranslation()
  const [selected, setSelected] = useState<string[]>([])
  const [pasteFor, setPasteFor] = useState<NewsroomArticle | null>(null)
  const [mergeOpen, setMergeOpen] = useState(false)
  const detail = useQuery({
    queryKey: newsroomAdminKeys.event(event.id),
    queryFn: () => browserNewsroomAdminClient().eventDetail(event.id),
  })
  const affected = (eventIds: string[]) => [
    newsroomAdminKeys.day(date),
    newsroomAdminKeys.editionsOfDate(date),
    ...eventIds.map(newsroomAdminKeys.event),
  ]
  const split = useNewsroomAction({
    locale,
    run: (client, articleIds: string[], csrf) =>
      client.splitEvent(event.id, articleIds, csrf),
    invalidates: (_, result) => affected([event.id, result.event_id]),
    onSuccess: () => setSelected([]),
  })
  const paste = useNewsroomAction({
    locale,
    run: (client, input: { articleId: string; body: string }, csrf) =>
      client.setManualBody(input.articleId, input.body, csrf),
    invalidates: () => affected([event.id]),
    onSuccess: () => setPasteFor(null),
  })
  const merge = useNewsroomAction({
    locale,
    run: (client, sourceIds: string[], csrf) =>
      client.mergeEvents(event.id, sourceIds, csrf),
    invalidates: sourceIds => affected([event.id, ...sourceIds]),
    onSuccess: () => setMergeOpen(false),
  })
  const error = split.error || merge.error

  if (detail.isPending) {
    return (
      <div role="status" aria-live="polite" className="grid gap-2 pt-3">
        <span className="sr-only">{t("newsroomAdminArticlesLoading")}</span>
        <div className="h-16 animate-pulse rounded-lg bg-link-hover" />
        <div className="h-16 animate-pulse rounded-lg bg-link-hover" />
      </div>
    )
  }
  if (!detail.data) {
    return (
      <div role="alert" className="flex items-center gap-3 pt-3 text-sm">
        <span className="font-bold text-destructive">
          {t("newsroomAdminArticlesLoadFailed")}
        </span>
        <button type="button" onClick={() => void detail.refetch()}>
          {t("newsroomAdminRetry")}
        </button>
      </div>
    )
  }

  const { articles, placements } = detail.data
  const toggle = (articleId: string) =>
    setSelected(current =>
      current.includes(articleId)
        ? current.filter(id => id !== articleId)
        : [...current, articleId]
    )
  const canSplit = selected.length > 0 && selected.length < articles.length

  return (
    <section
      className="grid gap-3 border-t border-line pt-3"
      aria-label={t("newsroomAdminOriginalArticles")}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs text-sea-ink-soft">
        <span>{t("newsroomAdminPlacedIn")}</span>
        {placements.length === 0 ? (
          <span>{t("newsroomAdminNotPlaced")}</span>
        ) : (
          placements.map(placement => (
            <StatusBadge
              key={placement.item_id}
              tone={placement.market_code === market ? "positive" : "neutral"}
            >
              {t(`newsroomAdminMarket_${placement.market_code}`)}
              {placement.removed ? ` · ${t("newsroomAdminRemoved")}` : ""}
              {placement.hidden ? ` · ${t("newsroomAdminHidden")}` : ""}
            </StatusBadge>
          ))
        )}
      </div>
      <ul className="m-0 grid list-none gap-2 p-0">
        {articles.map(article => (
          <li
            key={article.id}
            className="grid gap-2 rounded-lg border border-line bg-surface p-3"
          >
            <div className="flex items-start gap-3">
              <input
                type="checkbox"
                className="mt-1 size-4 w-auto shrink-0"
                checked={selected.includes(article.id)}
                onChange={() => toggle(article.id)}
                aria-label={t("newsroomAdminSelectArticle", {
                  title: article.title,
                })}
              />
              <div className="grid min-w-0 flex-1 gap-1">
                <a
                  href={article.url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-bold wrap-anywhere text-sea-ink"
                >
                  {article.title}
                </a>
                <p className="m-0 flex flex-wrap items-center gap-2 text-xs text-sea-ink-soft">
                  <span>{article.source_name}</span>
                  {article.published_at ? (
                    <span>{formatTimestamp(article.published_at)}</span>
                  ) : null}
                  <BodyStatus article={article} />
                  {article.triage_status !== "done" ? (
                    <StatusBadge
                      tone={
                        article.triage_status === "failed"
                          ? "danger"
                          : "caution"
                      }
                    >
                      {t(`newsroomAdminTriage_${article.triage_status}`)}
                    </StatusBadge>
                  ) : null}
                </p>
              </div>
              {article.body_status !== "ok" ? (
                <button
                  type="button"
                  className="shrink-0 text-sm"
                  onClick={() => {
                    paste.reset()
                    setPasteFor(article)
                  }}
                >
                  {t("newsroomAdminPasteBody")}
                </button>
              ) : null}
            </div>
            {article.body_preview ? (
              <details className="text-sm">
                <summary className="cursor-pointer font-bold text-lagoon-deep">
                  {t("newsroomAdminBodyPreview", {
                    count: article.body_length,
                  })}
                </summary>
                <p className="mt-2 mb-0 max-h-72 overflow-y-auto leading-6 whitespace-pre-wrap text-sea-ink-soft">
                  {article.body_preview}
                </p>
              </details>
            ) : article.feed_summary ? (
              <p className="m-0 text-sm leading-6 text-sea-ink-soft">
                {article.feed_summary}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={!canSplit || split.isPending}
          onClick={() => void split.run(selected)}
        >
          {t("newsroomAdminSplit", { count: selected.length })}
        </button>
        <button
          type="button"
          disabled={mergeOptions.length === 0}
          onClick={() => {
            merge.reset()
            setMergeOpen(true)
          }}
        >
          {t("newsroomAdminMergeInto")}
        </button>
      </div>
      {error ? (
        <p role="alert" className="m-0 font-bold text-destructive">
          {error}
        </p>
      ) : null}
      {pasteFor ? (
        <TextEditDialog
          open
          title={t("newsroomAdminPasteBodyTitle")}
          description={pasteFor.title}
          label={t("newsroomAdminBody")}
          initial=""
          maxLength={BODY_MAX}
          pending={paste.isPending}
          error={paste.error}
          onClose={() => setPasteFor(null)}
          onSubmit={body => void paste.run({ articleId: pasteFor.id, body })}
        />
      ) : null}
      {mergeOpen ? (
        <MergeDialog
          options={mergeOptions}
          pending={merge.isPending}
          error={merge.error}
          onClose={() => setMergeOpen(false)}
          onSubmit={sourceIds => void merge.run(sourceIds)}
        />
      ) : null}
    </section>
  )
}

function BodyStatus({ article }: { article: NewsroomArticle }) {
  const { t } = useTranslation()
  const tone =
    article.body_status === "ok"
      ? "positive"
      : article.body_status === "pending"
        ? "neutral"
        : "caution"
  return (
    <StatusBadge tone={tone}>
      {t(`newsroomAdminBody_${article.body_status}`)}
      {article.body_source === "manual"
        ? ` · ${t("newsroomAdminBodyManual")}`
        : ""}
    </StatusBadge>
  )
}

function MergeDialog({
  options,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  options: MergeOption[]
  pending: boolean
  error: string
  onClose: () => void
  onSubmit: (sourceIds: string[]) => void
}) {
  const { t } = useTranslation()
  const titleId = useId()
  const [chosen, setChosen] = useState<string[]>([])
  return (
    <Dialog open onClose={onClose} labelledBy={titleId}>
      <form
        className="grid gap-4"
        onSubmit={event => {
          event.preventDefault()
          if (chosen.length > 0) onSubmit(chosen)
        }}
      >
        <h2 id={titleId} className="m-0 text-lg font-extrabold">
          {t("newsroomAdminMergeTitle")}
        </h2>
        <p className="m-0 text-sm text-sea-ink-soft">
          {t("newsroomAdminMergeDescription")}
        </p>
        <fieldset className="m-0 grid max-h-80 gap-2 overflow-y-auto border-0 p-0">
          <legend className="sr-only">{t("newsroomAdminMergeTitle")}</legend>
          {options.map(option => (
            <label
              key={option.id}
              className="flex items-start gap-3 rounded-lg border border-line p-3 font-normal"
            >
              <input
                type="checkbox"
                className="mt-1 size-4 w-auto shrink-0"
                checked={chosen.includes(option.id)}
                onChange={() =>
                  setChosen(current =>
                    current.includes(option.id)
                      ? current.filter(id => id !== option.id)
                      : [...current, option.id]
                  )
                }
              />
              <span>{option.headline_zh_hant ?? option.working_title}</span>
            </label>
          ))}
        </fieldset>
        {error ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-3">
          <button type="button" onClick={onClose}>
            {t("dismiss")}
          </button>
          <button
            type="submit"
            className="primary-action"
            disabled={pending || chosen.length === 0}
          >
            {t("newsroomAdminMergeAction", { count: chosen.length })}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
