import {
  type DataManagementRun,
  type Locale,
  type NewsAdminCandidate,
  type NewsAdminEdition,
  type NewsAdminItem,
} from "@daily-insights/api-client"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { LoaderCircle } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { formatTimestamp } from "#/lib/format"
import {
  activePublishRunIds,
  type CandidateFilter,
  candidateFilters,
  filterCandidates,
  isActiveRun,
  isNewsRun,
  newsMutationErrorKey,
  publishRunCounts,
} from "#/lib/news-curation"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

const catalogKey = ["data-management", "catalog"] as const
const runsKey = ["data-management", "runs"] as const
const editionsKey = (date: string) => ["news-admin", "editions", date] as const

type NewsMarketCode = "global" | "tw_equity" | "us_equity"
type RerunInput =
  | { operation: "news_all" }
  | { operation: "news_market"; market_code: NewsMarketCode }

export function NewsManagementPage({ locale }: { locale: Locale }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const allRef = useRef<HTMLButtonElement>(null)
  const catalog = useQuery({
    queryKey: catalogKey,
    queryFn: () => browserAdministrationClient().dataManagementCatalog(),
  })
  const runs = useQuery({
    queryKey: runsKey,
    queryFn: () => browserAdministrationClient().listNewsDataManagementRuns(),
    refetchInterval: query =>
      query.state.data?.items.some(run => isNewsRun(run) && isActiveRun(run))
        ? 2_000
        : false,
  })
  useEffect(() => {
    if (catalog.error) void redirectExpired(catalog.error)
  }, [catalog.error, redirectExpired])
  useEffect(() => {
    if (runs.error) void redirectExpired(runs.error)
  }, [redirectExpired, runs.error])
  const newsRuns = runs.data?.items.filter(isNewsRun) ?? []
  // Only an administrator's own run blocks the rerun buttons: the scheduled
  // 08:00 run and its retries coexist with manual requests.
  const manualActive = newsRuns.some(
    run => run.requested_by_user_id !== null && isActiveRun(run)
  )
  const enqueue = useMutation({
    mutationFn: async (input: RerunInput) =>
      browserAdministrationClient().createDataManagementRun(
        input,
        await requireCsrfToken()
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: runsKey })
      setConfirmOpen(false)
    },
  })
  const cancelRun = useMutation({
    mutationFn: async (runId: string) =>
      browserAdministrationClient().cancelDataManagementRun(
        runId,
        await requireCsrfToken()
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: runsKey }),
  })
  const errorMessage = enqueue.error
    ? t(newsMutationErrorKey(enqueue.error, "newsManagementFailed"))
    : ""
  const submit = async (input: RerunInput) => {
    try {
      await enqueue.mutateAsync(input)
    } catch (caught) {
      await redirectExpired(caught)
    }
  }
  const closeConfirmation = () => {
    setConfirmOpen(false)
    window.requestAnimationFrame(() => allRef.current?.focus())
  }

  if (catalog.isPending || runs.isPending) {
    return (
      <main className="page-shell" role="status" aria-live="polite">
        <span className="sr-only">{t("newsManagementLoading")}</span>
        <div className="h-10 w-72 animate-pulse rounded bg-link-hover" />
        <div className="mt-6 h-64 max-w-4xl animate-pulse rounded-xl bg-link-hover" />
      </main>
    )
  }

  if (catalog.error || runs.error) {
    return (
      <main className="page-shell">
        <p role="alert" className="font-bold text-market-up">
          {t("newsManagementLoadFailed")}
        </p>
      </main>
    )
  }

  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("newsManagementTitle")}
        </h1>
        <p className="mt-3 leading-7 text-sea-ink-soft">
          {t("newsManagementDescription")}
        </p>
      </header>
      {errorMessage ? (
        <p role="alert" className="mb-4 font-bold text-market-up">
          {errorMessage}
        </p>
      ) : null}
      <div className="grid max-w-4xl gap-5 md:grid-cols-2">
        <section className="surface-panel p-5" aria-labelledby="news-all-title">
          <h2 id="news-all-title" className="m-0 text-lg font-extrabold">
            {t("newsManagementAll")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
            {t("newsManagementAllDescription")}
          </p>
          <button
            ref={allRef}
            type="button"
            className="primary-action mt-4"
            disabled={
              manualActive ||
              enqueue.isPending ||
              !catalog.data?.daily_news_enabled
            }
            onClick={() => setConfirmOpen(true)}
          >
            {t("newsManagementAllAction")}
          </button>
        </section>
        <section
          className="surface-panel p-5"
          aria-labelledby="news-market-title"
        >
          <h2 id="news-market-title" className="m-0 text-lg font-extrabold">
            {t("newsManagementMarket")}
          </h2>
          <div className="mt-4 grid gap-2">
            {catalog.data?.news_markets.map(market => (
              <button
                key={market}
                type="button"
                className="secondary-action text-left"
                disabled={
                  manualActive ||
                  enqueue.isPending ||
                  !catalog.data?.daily_news_enabled
                }
                onClick={() =>
                  void submit({ operation: "news_market", market_code: market })
                }
              >
                {t(`newsEdition_${market}`)}
              </button>
            ))}
          </div>
        </section>
      </div>
      <section
        className="surface-panel mt-6 max-w-4xl p-5"
        aria-labelledby="news-runs-title"
      >
        <h2 id="news-runs-title" className="m-0 text-lg font-extrabold">
          {t("newsManagementLatest")}
        </h2>
        <div className="mt-4 grid gap-2">
          {newsRuns.map(run => (
            <details key={run.id} className="rounded-md border border-line p-3">
              <summary className="cursor-pointer font-bold">
                {run.status} · <RunLabel run={run} /> · {run.edition_date}
              </summary>
              <p className="mt-3 text-sm text-sea-ink-soft">
                <RunOutcome run={run} />
              </p>
              {isActiveRun(run) ? (
                <button
                  type="button"
                  className="secondary-action mt-3"
                  disabled={cancelRun.isPending}
                  onClick={() =>
                    void cancelRun.mutateAsync(run.id).catch(redirectExpired)
                  }
                >
                  {t("dataManagementCancel")}
                </button>
              ) : null}
            </details>
          ))}
        </div>
      </section>
      <NewsCuration
        taipeiDate={catalog.data.taipei_date}
        dailyNewsEnabled={catalog.data.daily_news_enabled}
        markets={catalog.data.news_markets}
        runs={newsRuns}
        redirectExpired={redirectExpired}
      />
      <Dialog
        open={confirmOpen}
        onClose={closeConfirmation}
        labelledBy="news-confirmation"
        initialFocusRef={cancelRef}
        role="alertdialog"
      >
        <h2 id="news-confirmation" className="m-0 text-lg font-extrabold">
          {t("newsManagementConfirmTitle")}
        </h2>
        <p className="mt-3 text-sm leading-6 text-sea-ink-soft">
          {t("newsManagementConfirmWarning", {
            date: catalog.data?.taipei_date,
          })}
        </p>
        <div className="mt-5 flex justify-end gap-3">
          <button
            ref={cancelRef}
            type="button"
            className="secondary-action"
            onClick={closeConfirmation}
          >
            {t("dismiss")}
          </button>
          <button
            type="button"
            className="primary-action"
            disabled={enqueue.isPending}
            onClick={() => void submit({ operation: "news_all" })}
          >
            {enqueue.isPending ? (
              <LoaderCircle
                className="mr-2 inline size-4 animate-spin"
                aria-hidden="true"
              />
            ) : null}
            {t("newsManagementConfirmAction")}
          </button>
        </div>
      </Dialog>
    </main>
  )
}

function RunLabel({ run }: { run: DataManagementRun }) {
  const { t } = useTranslation()
  if (run.operation === "news_all") return t("newsManagementAllMarkets")
  if (run.operation === "news_publish") return t("newsManagementPublishRun")
  return t(`newsEdition_${run.market_code}`)
}

function RunOutcome({ run }: { run: DataManagementRun }) {
  const { t } = useTranslation()
  if (run.error) return run.error
  if (run.operation === "news_publish") {
    const counts = publishRunCounts(run.result)
    return counts ? t("newsCurationPublishResult", counts) : ""
  }
  return String(run.result?.outcome ?? "")
}

type NewsCurationProps = {
  taipeiDate: string
  dailyNewsEnabled: boolean
  markets: readonly NewsMarketCode[]
  runs: readonly DataManagementRun[]
  redirectExpired: (error: unknown) => Promise<boolean>
}

/** Candidate monitoring and manual curation for one edition date. */
function NewsCuration({
  taipeiDate,
  dailyNewsEnabled,
  markets,
  runs,
  redirectExpired,
}: NewsCurationProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [date, setDate] = useState(taipeiDate)
  const [market, setMarket] = useState<NewsMarketCode>(markets[0] ?? "global")
  const [filter, setFilter] = useState<CandidateFilter>("all")
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  // The error banner describes the most recent action only; a stale error
  // from the other mutation must not outlive a later success.
  const [lastAction, setLastAction] = useState<"hide" | "publish" | null>(null)
  const anyNewsRunActive = runs.some(isActiveRun)
  const publishActive = activePublishRunIds(runs).size > 0
  const editions = useQuery({
    queryKey: editionsKey(date),
    queryFn: () => browserAdministrationClient().listNewsEditions(date),
    enabled: date.length > 0,
    refetchInterval: anyNewsRunActive ? 2_000 : false,
  })
  // A finished run may have changed the edition after the last poll, so refetch
  // once more when the queue goes idle.
  const wasActive = useRef(anyNewsRunActive)
  useEffect(() => {
    if (wasActive.current && !anyNewsRunActive) {
      void queryClient.invalidateQueries({ queryKey: ["news-admin"] })
    }
    wasActive.current = anyNewsRunActive
  }, [anyNewsRunActive, queryClient])
  useEffect(() => {
    if (editions.error) void redirectExpired(editions.error)
  }, [editions.error, redirectExpired])
  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["news-admin"] }),
      queryClient.invalidateQueries({ queryKey: runsKey }),
    ])
  const toggleHidden = useMutation({
    mutationFn: async (item: NewsAdminItem) => {
      const csrfToken = await requireCsrfToken()
      const client = browserAdministrationClient()
      return item.hidden
        ? client.unhideNewsItem(item.id, csrfToken)
        : client.hideNewsItem(item.id, csrfToken)
    },
    onMutate: () => setLastAction("hide"),
    onSuccess: () => void invalidate(),
  })
  const publish = useMutation({
    mutationFn: async (input: {
      edition_id: string
      candidate_ids: string[]
    }) =>
      browserAdministrationClient().publishNewsCandidates(
        input,
        await requireCsrfToken()
      ),
    onMutate: () => setLastAction("publish"),
    onSuccess: () => {
      setSelected(new Set())
      void invalidate()
    },
  })
  const mutationError =
    lastAction === "publish"
      ? publish.error
      : lastAction === "hide"
        ? toggleHidden.error
        : null
  const errorMessage = mutationError
    ? t(newsMutationErrorKey(mutationError, "newsCurationPublishFailed"))
    : ""
  const edition = editions.data?.editions.find(
    entry => entry.market_code === market
  )
  // A rerun can replace the edition (new revision, new candidate ids) under
  // an open selection; ids from the old revision must not be submitted.
  const editionId = edition?.edition?.id
  useEffect(() => {
    setSelected(new Set())
  }, [editionId])
  const changeDate = (value: string) => {
    setDate(value)
    setSelected(new Set())
  }
  const changeMarket = (value: NewsMarketCode) => {
    setMarket(value)
    setSelected(new Set())
  }
  const toggleSelected = (id: string, checked: boolean) => {
    setSelected(current => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }
  const submitPublish = async () => {
    if (!edition?.edition) return
    try {
      await publish.mutateAsync({
        edition_id: edition.edition.id,
        candidate_ids: [...selected],
      })
    } catch (caught) {
      await redirectExpired(caught)
    }
  }
  const submitToggleHidden = async (item: NewsAdminItem) => {
    try {
      await toggleHidden.mutateAsync(item)
    } catch (caught) {
      await redirectExpired(caught)
    }
  }
  const publishDisabled =
    selected.size === 0 ||
    publish.isPending ||
    publishActive ||
    !dailyNewsEnabled ||
    date !== taipeiDate ||
    !edition?.edition

  return (
    <section
      className="surface-panel mt-6 max-w-6xl p-5"
      aria-labelledby="news-curation-title"
    >
      <h2 id="news-curation-title" className="m-0 text-lg font-extrabold">
        {t("newsCurationTitle")}
      </h2>
      <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
        {t("newsCurationDescription")}
      </p>
      <div className="mt-4 flex flex-wrap items-end gap-4">
        <label className="grid gap-1 text-xs font-bold text-sea-ink-soft">
          {t("newsCurationDate")}
          <input
            type="date"
            className="rounded-lg border border-chip-line bg-chip px-3 py-2 text-sm font-normal text-sea-ink"
            value={date}
            onChange={event => changeDate(event.target.value)}
          />
        </label>
        <div
          role="group"
          aria-label={t("newsCurationMarkets")}
          className="flex flex-wrap items-center gap-1"
        >
          {markets.map(code => (
            <ToggleButton
              key={code}
              pressed={code === market}
              onClick={() => changeMarket(code)}
            >
              {t(`newsEdition_${code}`)}
            </ToggleButton>
          ))}
        </div>
      </div>
      {errorMessage ? (
        <p role="alert" className="mt-4 font-bold text-market-up">
          {errorMessage}
        </p>
      ) : null}
      {date.length === 0 ? (
        <p className="mt-4 text-sm text-sea-ink-soft">
          {t("newsCurationPickDate")}
        </p>
      ) : editions.isPending ? (
        <p role="status" className="mt-4 text-sm text-sea-ink-soft">
          {t("newsCurationLoading")}
        </p>
      ) : editions.error ? (
        <p role="alert" className="mt-4 font-bold text-market-up">
          {t("newsCurationLoadFailed")}
        </p>
      ) : !edition?.edition ? (
        <p className="mt-4 text-sm text-sea-ink-soft">
          {t("newsCurationEmpty")}
        </p>
      ) : (
        <>
          <EditionSummary edition={edition} />
          <PublishedItems
            items={edition.items}
            pending={toggleHidden.isPending}
            onToggleHidden={item => void submitToggleHidden(item)}
          />
          <h3 className="mt-6 mb-0 text-base font-extrabold">
            {t("newsCurationCandidatesTitle")}
          </h3>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <div
              role="group"
              aria-label={t("newsCurationFilter")}
              className="flex flex-wrap items-center gap-1"
            >
              {candidateFilters.map(option => (
                <ToggleButton
                  key={option}
                  pressed={option === filter}
                  onClick={() => setFilter(option)}
                >
                  {t(`newsCurationFilter_${option}`)}
                </ToggleButton>
              ))}
            </div>
            <button
              type="button"
              className="primary-action"
              disabled={publishDisabled}
              onClick={() => void submitPublish()}
            >
              {publish.isPending ? (
                <LoaderCircle
                  className="mr-2 inline size-4 animate-spin"
                  aria-hidden="true"
                />
              ) : null}
              {selected.size > 0
                ? t("newsCurationPublishSelectedCount", {
                    count: selected.size,
                  })
                : t("newsCurationPublishSelected")}
            </button>
          </div>
          {date !== taipeiDate ? (
            <p className="mt-2 text-xs text-sea-ink-soft">
              {t("newsCurationPublishTodayOnly")}
            </p>
          ) : null}
          <CandidateTable
            candidates={filterCandidates(edition.candidates, filter)}
            selected={selected}
            selectable={!publishActive}
            queuedRunIds={activePublishRunIds(runs)}
            onToggle={toggleSelected}
          />
        </>
      )}
    </section>
  )
}

function ToggleButton({
  pressed,
  onClick,
  children,
}: {
  pressed: boolean
  onClick: () => void
  children: string
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={`rounded-lg border px-2.5 py-1 text-[11px] font-bold transition-colors duration-120 hover:border-lagoon/55 hover:text-palm ${pressed ? "border-lagoon/35 bg-lagoon/14 text-palm" : "border-line bg-surface text-sea-ink-soft"}`}
    >
      {children}
    </button>
  )
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: string
  tone?: "neutral" | "caution" | "muted"
}) {
  const toneClass =
    tone === "caution"
      ? "border-market-caution/50 bg-market-caution/10 text-market-caution"
      : tone === "muted"
        ? "border-line bg-link-hover text-sea-ink-soft"
        : "border-chip-line bg-chip text-sea-ink"
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-bold whitespace-nowrap ${toneClass}`}
    >
      {children}
    </span>
  )
}

const countKeys = [
  "discovered",
  "fetch_failed",
  "unused",
  "reviewed",
  "dropped",
  "published",
  "hidden",
] as const

function EditionSummary({ edition }: { edition: NewsAdminEdition }) {
  const { t } = useTranslation()
  const detail = edition.edition
  if (!detail) return null
  return (
    <div className="mt-4 rounded-md border border-line p-3">
      <p className="m-0 text-sm text-sea-ink-soft">
        {t("newsCurationRevision", { revision: detail.revision })} ·{" "}
        {t(`newsCurationStatus_${detail.status}`)} ·{" "}
        {t("newsCurationGeneratedAt", {
          time: formatTimestamp(detail.generated_at),
        })}{" "}
        · {t("newsCurationPromptVersion", { version: detail.prompt_version })}
      </p>
      <ul
        aria-label={t("newsCurationCounts")}
        className="m-0 mt-2 flex list-none flex-wrap gap-1.5 p-0"
      >
        {countKeys.map(key => (
          <li key={key}>
            <Badge tone="muted">
              {`${t(`newsCandidateStage_${key}`)} ${detail.counts[key]}`}
            </Badge>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ImportanceStars({ importance }: { importance: number }) {
  const { t } = useTranslation()
  // Model tags are not range-checked before they reach the admin view.
  const stars = Math.max(0, Math.min(5, Math.trunc(importance)))
  return (
    <span
      aria-label={t("dailyNewsImportance", { count: stars })}
      className="text-market-caution"
    >
      {"★".repeat(stars)}
    </span>
  )
}

function MarketTag({ market }: { market: string | null }) {
  const { t } = useTranslation()
  if (!market) return null
  // Candidate tags come straight from the model, so an unknown market falls
  // back to its raw code instead of a missing-key label.
  return <span>{t(`newsMarket_${market}`, { defaultValue: market })}</span>
}

function PublishedItems({
  items,
  pending,
  onToggleHidden,
}: {
  items: readonly NewsAdminItem[]
  pending: boolean
  onToggleHidden: (item: NewsAdminItem) => void
}) {
  const { t } = useTranslation()
  return (
    <>
      <h3 className="mt-6 mb-0 text-base font-extrabold">
        {t("newsCurationItemsTitle")}
      </h3>
      {items.length === 0 ? (
        <p className="mt-2 text-sm text-sea-ink-soft">
          {t("newsCurationItemsEmpty")}
        </p>
      ) : (
        <ul className="m-0 mt-3 grid list-none gap-2 p-0">
          {items.map(item => (
            <li
              key={item.id}
              className={`flex flex-wrap items-center justify-between gap-3 rounded-md border border-line p-3 ${item.hidden ? "opacity-60" : ""}`}
            >
              <div className="min-w-0 flex-1">
                <p className="m-0 text-sm font-bold">
                  <span className="mr-2 font-mono text-sea-ink-soft">
                    #{item.rank}
                  </span>
                  {item.headline}
                </p>
                <p className="m-0 mt-1 flex flex-wrap items-center gap-2 text-xs text-sea-ink-soft">
                  <span>{item.source_name}</span>
                  <MarketTag market={item.market} />
                  <ImportanceStars importance={item.importance} />
                  <Badge>{t(`newsCurationOrigin_${item.origin}`)}</Badge>
                  {item.hidden ? (
                    <Badge tone="caution">
                      {t("newsCandidateStage_hidden")}
                    </Badge>
                  ) : null}
                </p>
              </div>
              <button
                type="button"
                className="secondary-action"
                disabled={pending}
                aria-label={`${item.hidden ? t("newsCurationUnhide") : t("newsCurationHide")}: ${item.headline}`}
                onClick={() => onToggleHidden(item)}
              >
                {item.hidden ? t("newsCurationUnhide") : t("newsCurationHide")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function CandidateTable({
  candidates,
  selected,
  selectable,
  queuedRunIds,
  onToggle,
}: {
  candidates: readonly NewsAdminCandidate[]
  selected: ReadonlySet<string>
  selectable: boolean
  queuedRunIds: ReadonlySet<string>
  onToggle: (id: string, checked: boolean) => void
}) {
  const { t } = useTranslation()
  if (candidates.length === 0) {
    return (
      <p className="mt-3 text-sm text-sea-ink-soft">
        {t("newsCurationCandidatesEmpty")}
      </p>
    )
  }
  const headers = [
    "newsCurationColSelect",
    "newsCurationColHeadline",
    "newsCurationColSource",
    "newsCurationColSeen",
    "newsCurationColStage",
    "newsCurationColAi",
    "newsCurationColPublish",
  ]
  return (
    <div className="mt-3 overflow-x-auto rounded-md border border-line">
      <table className="w-full min-w-200 text-sm">
        <thead className="bg-link-hover text-left text-xs text-sea-ink-soft">
          <tr>
            {headers.map(key => (
              <th key={key} scope="col" className="px-3 py-2.5">
                {t(key)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {candidates.map(candidate => {
            const headlineId = `candidate-${candidate.id}`
            const published = candidate.stage === "published"
            return (
              <tr key={candidate.id} className="border-t border-line">
                <td className="px-3 py-2.5">
                  <input
                    type="checkbox"
                    aria-labelledby={headlineId}
                    checked={selected.has(candidate.id)}
                    disabled={published || !selectable}
                    onChange={event =>
                      onToggle(candidate.id, event.target.checked)
                    }
                  />
                </td>
                <td className="px-3 py-2.5">
                  <a
                    id={headlineId}
                    href={candidate.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-bold"
                  >
                    {candidate.headline}
                  </a>
                </td>
                <td className="px-3 py-2.5 text-sea-ink-soft">
                  {candidate.source_name}
                </td>
                <td className="px-3 py-2.5 font-mono text-xs tabular-nums text-sea-ink-soft">
                  {candidate.seen_at ? formatTimestamp(candidate.seen_at) : "—"}
                </td>
                <td className="px-3 py-2.5">
                  <span className="flex flex-wrap gap-1">
                    <Badge
                      tone={
                        candidate.stage === "dropped" ? "caution" : "neutral"
                      }
                    >
                      {t(`newsCandidateStage_${candidate.stage}`)}
                    </Badge>
                    {candidate.drop_reason ? (
                      <Badge tone="muted">
                        {t(`newsCandidateDrop_${candidate.drop_reason}`)}
                      </Badge>
                    ) : null}
                  </span>
                </td>
                <td className="px-3 py-2.5">
                  {candidate.ai_rank === null ? (
                    "—"
                  ) : (
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-mono tabular-nums">
                        #{candidate.ai_rank}
                      </span>
                      <MarketTag market={candidate.ai_market} />
                      {candidate.ai_importance !== null ? (
                        <ImportanceStars importance={candidate.ai_importance} />
                      ) : null}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2.5 text-xs">
                  {candidate.publish_error ? (
                    <span className="text-market-up">
                      {candidate.publish_error}
                    </span>
                  ) : candidate.publish_run_id &&
                    queuedRunIds.has(candidate.publish_run_id) ? (
                    <Badge tone="caution">
                      {t("newsCurationPublishQueued")}
                    </Badge>
                  ) : null}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
