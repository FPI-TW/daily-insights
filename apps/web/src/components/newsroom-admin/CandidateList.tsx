import { useTranslation } from "react-i18next"
import type { NewsroomCandidate } from "#/lib/newsroom-admin"
import { StatusBadge } from "./StatusBadge"

/** Events of the window not in this edition, by this market's score (D19). */
export function CandidateList({
  candidates,
  canAdd,
  pendingEventId,
  onAdd,
}: {
  candidates: NewsroomCandidate[]
  canAdd: boolean
  pendingEventId: string | null
  onAdd: (eventId: string) => void
}) {
  const { t } = useTranslation()
  if (candidates.length === 0) {
    return (
      <p className="m-0 rounded-xl border border-dashed border-line p-6 text-center text-sea-ink-soft">
        {t("newsroomAdminNoCandidates")}
      </p>
    )
  }
  return (
    <ol className="m-0 grid list-none gap-2 p-0">
      {candidates.map(({ event, score }) => (
        <li
          key={event.id}
          className="flex items-start gap-3 rounded-lg border border-line bg-surface p-3"
        >
          <span className="min-w-12 shrink-0 text-right text-sm font-extrabold tabular-nums">
            {Math.round(score)}
          </span>
          <div className="grid min-w-0 flex-1 gap-1">
            <p className="m-0 font-bold">
              {event.headline_zh_hant ?? event.working_title}
            </p>
            <p className="m-0 flex flex-wrap items-center gap-2 text-xs text-sea-ink-soft">
              <span>
                {t("newsroomAdminCoverage", {
                  articles: event.article_count,
                  sources: event.source_count,
                })}
              </span>
              {event.body_ok_count === 0 ? (
                <StatusBadge tone="danger">
                  {t("newsroomAdminAlert_missingBody")}
                </StatusBadge>
              ) : null}
              {event.analysis_status === "failed" ? (
                <StatusBadge tone="danger">
                  {t("newsroomAdminAlert_analysisFailed")}
                </StatusBadge>
              ) : event.analysis_status === "ready" ? (
                <StatusBadge tone="positive">
                  {t("newsroomAdminAnalysisReady")}
                </StatusBadge>
              ) : null}
            </p>
            {event.articles.length > 0 ? (
              <p className="m-0 text-xs wrap-anywhere text-sea-ink-soft">
                {event.articles
                  .slice(0, 3)
                  .map(article => article.source_name)
                  .join(" · ")}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            className="shrink-0"
            disabled={!canAdd || pendingEventId !== null}
            aria-label={t("newsroomAdminAddCandidateLabel", {
              title: event.headline_zh_hant ?? event.working_title,
            })}
            onClick={() => onAdd(event.id)}
          >
            {t("newsroomAdminAddCandidate")}
          </button>
        </li>
      ))}
    </ol>
  )
}
