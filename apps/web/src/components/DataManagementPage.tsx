import {
  ApiError,
  type JobRun,
  type Locale,
  type RoutineRun,
} from "@daily-insights/api-client"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

import { JobKindBadge, StatusBadge } from "./DataManagementBadges"
import { groupRuns, recentRoutines } from "./data-management-display"

const catalogKey = ["orchestration", "catalog"] as const
const runsKey = ["orchestration", "job-runs"] as const
const routinesKey = ["orchestration", "routine-runs"] as const
const ACTIVE = new Set(["pending", "running"])
const MARKET_JOBS = [
  {
    key: "global_macro_refresh",
    label: "reportMarket_global_macro_bonds",
    action: "dataManagementMacroAction",
  },
  {
    key: "us_equity_refresh",
    label: "reportMarket_us_equity",
    action: "dataManagementIndexAction",
  },
  {
    key: "tw_equity_refresh",
    label: "reportMarket_tw_equity",
    action: "dataManagementInstitutionalAction",
  },
] as const

export function DataManagementPage({ locale }: { locale: Locale }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const redirectExpired = useSessionExpiryRedirect(locale, "admin")
  const [page, setPage] = useState(1)
  const catalog = useQuery({
    queryKey: catalogKey,
    queryFn: () => browserAdministrationClient().orchestrationCatalog(),
  })
  const runs = useQuery({
    queryKey: [...runsKey, page],
    queryFn: () => browserAdministrationClient().listJobRuns(page),
    refetchInterval: query =>
      query.state.data?.items.some(run => ACTIVE.has(run.status))
        ? 2_000
        : false,
  })
  const routines = useQuery({
    queryKey: routinesKey,
    queryFn: () => browserAdministrationClient().listRoutineRuns(),
    refetchInterval: query =>
      query.state.data?.items.some(routine => ACTIVE.has(routine.status))
        ? 2_000
        : false,
  })
  const enqueue = useMutation({
    mutationFn: async (jobKey: string) =>
      browserAdministrationClient().createJobRun(
        jobKey,
        await requireCsrfToken()
      ),
    onSuccess: () => {
      setPage(1)
      void queryClient.invalidateQueries({ queryKey: runsKey })
    },
  })
  const cancel = useMutation({
    mutationFn: async (runId: string) =>
      browserAdministrationClient().cancelJobRun(
        runId,
        await requireCsrfToken()
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: runsKey }),
  })

  if (catalog.isPending || runs.isPending || routines.isPending) {
    return (
      <main className="page-shell" role="status" aria-live="polite">
        <span className="sr-only">{t("dataManagementLoading")}</span>
        <div className="h-10 w-72 animate-pulse rounded bg-link-hover" />
        <div className="mt-6 h-64 max-w-5xl animate-pulse rounded-xl bg-link-hover" />
      </main>
    )
  }

  const failure =
    catalog.error ??
    runs.error ??
    routines.error ??
    enqueue.error ??
    cancel.error
  const errorMessage =
    failure instanceof ApiError && failure.status === 503
      ? t("dataManagementUnavailable")
      : failure
        ? t("dataManagementFailed")
        : null
  const visibleRoutines = catalog.data
    ? recentRoutines(routines.data?.items ?? [], catalog.data.taipei_date)
    : []
  const runGroups = groupRuns(runs.data?.items ?? [])
  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("dataManagementTitle")}
        </h1>
        <p className="mt-3 leading-7 text-sea-ink-soft">
          {t("dataManagementDescription")}
        </p>
      </header>
      {errorMessage ? (
        <p role="alert" className="mb-4 font-bold text-market-up">
          {errorMessage}
        </p>
      ) : null}
      <section className="grid max-w-5xl gap-5 md:grid-cols-3">
        {MARKET_JOBS.map(job => (
          <article key={job.key} className="surface-panel p-5">
            <h2 className="m-0 text-lg font-extrabold">{t(job.label)}</h2>
            <p className="mt-2 text-sm leading-6 text-sea-ink-soft">
              {t("dataManagementProviderDescription")}
            </p>
            <button
              type="button"
              className="primary-action mt-4"
              disabled={enqueue.isPending}
              onClick={() =>
                void enqueue.mutateAsync(job.key).catch(redirectExpired)
              }
            >
              {t(job.action)}
            </button>
          </article>
        ))}
      </section>
      <section className="surface-panel mt-6 max-w-5xl p-5">
        <h2 className="m-0 text-lg font-extrabold">
          {t("dataManagementRoutines")}
        </h2>
        <p className="mt-2 text-sm text-sea-ink-soft">
          {t("dataManagementRecentDays")}
        </p>
        <div className="mt-4 grid gap-3" aria-live="polite">
          {visibleRoutines.length ? (
            visibleRoutines.map(routine => (
              <RoutineCard key={routine.id} routine={routine} locale={locale} />
            ))
          ) : (
            <p className="m-0 text-sm text-sea-ink-soft">
              {t("dataManagementNoRoutines")}
            </p>
          )}
        </div>
      </section>
      <section className="surface-panel mt-6 max-w-5xl p-5">
        <h2 className="m-0 text-lg font-extrabold">
          {t("dataManagementLatest")}
        </h2>
        <div className="mt-4 grid gap-2" aria-live="polite">
          {runGroups.map(group => (
            <article
              key={group.id}
              className="min-w-0 rounded-md border-2 border-line p-3"
            >
              {group.missingIds.length ? (
                <p className="text-xs break-all text-sea-ink-soft">
                  {t("dataManagementMissingLinked", {
                    jobs: group.missingIds.join(", "),
                  })}
                </p>
              ) : null}
              <ol className="m-0 grid list-none gap-2 p-0">
                {group.runs.map((run, index) => (
                  <li key={run.id} className="min-w-0">
                    <RunCard
                      run={run}
                      ordinal={index + 1}
                      locale={locale}
                      cancelling={cancel.isPending}
                      onCancel={() =>
                        void cancel.mutateAsync(run.id).catch(redirectExpired)
                      }
                    />
                  </li>
                ))}
              </ol>
            </article>
          ))}
        </div>
        <div className="mt-5 flex items-center justify-between gap-3">
          <button
            type="button"
            className="secondary-action"
            disabled={page === 1}
            onClick={() => setPage(current => Math.max(1, current - 1))}
          >
            {t("dataManagementPagePrevious")}
          </button>
          <span className="text-sm text-sea-ink-soft">
            {t("dataManagementPageStatus", { page })}
          </span>
          <button
            type="button"
            className="secondary-action"
            disabled={!runs.data?.has_more}
            onClick={() => setPage(current => current + 1)}
          >
            {t("dataManagementPageNext")}
          </button>
        </div>
      </section>
    </main>
  )
}

function RoutineCard({
  routine,
  locale,
}: {
  routine: RoutineRun
  locale: Locale
}) {
  const { t } = useTranslation()
  const jobNames = new Map(routine.jobs.map(job => [job.id, job.job_key]))
  const format = (value: string) =>
    new Intl.DateTimeFormat(locale, {
      dateStyle: "short",
      timeStyle: "short",
      timeZone: "Asia/Taipei",
    }).format(new Date(value))
  return (
    <details className="min-w-0 rounded-md border border-line p-3 wrap-break-word">
      <summary className="cursor-pointer font-bold wrap-break-word">
        <StatusBadge status={routine.status} /> · {routine.routine_key} ·{" "}
        {routine.edition_date}
      </summary>
      <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
        {t("dataManagementRoutineSchedule", {
          scheduled: format(routine.scheduled_for),
          deadline: format(routine.deadline_at),
        })}
      </p>
      <ol className="mt-3 grid list-none gap-3 p-0">
        {routine.jobs.map(job => (
          <li key={job.id} className="rounded border border-line p-3">
            <p className="m-0 font-bold">
              <JobKindBadge kind={job.kind} jobKey={job.job_key} /> ·{" "}
              {job.job_key} · <StatusBadge status={job.status} />
            </p>
            {job.depends_on.length ? (
              <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
                {t("dataManagementDependsOn", {
                  jobs: job.depends_on
                    .map(id => jobNames.get(id) ?? id)
                    .join(", "),
                })}
              </p>
            ) : null}
            {job.functions.map(functionRun => (
              <div key={functionRun.id} className="mt-2 pl-3 text-sm">
                <p className="m-0 text-sea-ink-soft">
                  {functionRun.provider_key} · {functionRun.function_key} ·{" "}
                  <StatusBadge status={functionRun.status} />
                </p>
                {functionRun.attempts.map(attempt => (
                  <p
                    key={attempt.id}
                    className="mt-1 mb-0 pl-3 text-xs text-sea-ink-soft"
                  >
                    {t("dataManagementAttempt", {
                      number: attempt.attempt_number,
                    })}
                    {" · "}
                    <StatusBadge status={attempt.status} />
                  </p>
                ))}
              </div>
            ))}
            {job.error ? (
              <p className="mt-2 mb-0 text-sm text-market-up">{job.error}</p>
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  )
}

function RunCard({
  run,
  ordinal,
  locale,
  cancelling,
  onCancel,
}: {
  run: JobRun
  ordinal: number
  locale: Locale
  cancelling: boolean
  onCancel: () => void
}) {
  const { t } = useTranslation()
  return (
    <details className="min-w-0 rounded-md border border-line p-3 wrap-break-word">
      <summary className="cursor-pointer font-bold wrap-break-word">
        {t("dataManagementSequenceStep", { number: ordinal })} ·{" "}
        <JobKindBadge kind={run.kind} jobKey={run.job_key} /> ·{" "}
        <StatusBadge status={run.status} /> · {run.job_key} · {run.edition_date}
        <span className="mt-1 block text-xs font-normal text-sea-ink-soft">
          {t(
            run.started_at
              ? "dataManagementStartedAt"
              : "dataManagementQueuedAt",
            {
              time: new Intl.DateTimeFormat(locale, {
                dateStyle: "short",
                timeStyle: "medium",
                timeZone: "Asia/Taipei",
              }).format(new Date(run.started_at ?? run.created_at)),
            }
          )}
        </span>
        {run.completed_at ? (
          <span className="mt-1 block text-xs font-normal text-sea-ink-soft">
            {t("dataManagementCompletedAt", {
              time: new Intl.DateTimeFormat(locale, {
                dateStyle: "short",
                timeStyle: "medium",
                timeZone: "Asia/Taipei",
              }).format(new Date(run.completed_at)),
            })}
          </span>
        ) : null}
      </summary>
      <div className="mt-3 grid gap-2 text-sm text-sea-ink-soft">
        {run.functions.map(item => (
          <p key={item.id} className="m-0">
            {item.provider_key} · {item.function_key} ·{" "}
            <StatusBadge status={item.status} />
          </p>
        ))}
        {run.error ? <p className="m-0 text-market-up">{run.error}</p> : null}
      </div>
      {ACTIVE.has(run.status) ? (
        <button
          type="button"
          className="secondary-action mt-3"
          disabled={cancelling}
          onClick={onCancel}
        >
          {t("dataManagementCancel")}
        </button>
      ) : null}
    </details>
  )
}
