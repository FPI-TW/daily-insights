import { ClientOnly, Link, useRouter } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import type {
  ProvisionalReport,
  ReportBlock,
  ReportStatus,
  ReportValue,
} from "#/lib/provisional-reports"

const statusStyles: Record<ReportStatus, string> = {
  complete: "border-lagoon/40 bg-lagoon/10 text-sea-ink",
  partial: "border-amber-500/45 bg-amber-500/10 text-sea-ink",
  unavailable: "border-red-500/45 bg-red-500/10 text-sea-ink",
}

export function ReportLoadingScreen() {
  const { t } = useTranslation()
  return (
    <main className="page-shell" role="status" aria-live="polite">
      <p className="sr-only">{t("reportLoadingAnnouncement")}</p>
      <div className="animate-pulse space-y-5">
        <div className="h-4 w-28 rounded bg-line" />
        <div className="h-10 w-72 max-w-full rounded bg-line" />
        <div className="grid gap-px overflow-hidden rounded-xl border border-line md:grid-cols-2">
          <div className="h-40 bg-surface" />
          <div className="h-40 bg-surface" />
        </div>
      </div>
    </main>
  )
}

function StatusBadge({ status }: { status: ReportStatus }) {
  const { t } = useTranslation()
  return (
    <span
      className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-extrabold ${statusStyles[status]}`}
    >
      {t(`reportStatus_${status}`)}
    </span>
  )
}

function BlockStatusBadge({ status }: { status: ReportBlock["status"] }) {
  const { t } = useTranslation()
  const style =
    status === "error"
      ? "border-red-500/45 bg-red-500/10"
      : status === "missing"
        ? "border-amber-500/40 bg-amber-500/10"
        : "border-lagoon/40 bg-lagoon/10"
  return (
    <span
      className={`rounded-full border px-2 py-1 text-xs font-bold text-sea-ink ${style}`}
    >
      {t(`reportBlockStatus_${status}`)}
    </span>
  )
}

function reportValue(value: ReportValue | null, t: (key: string) => string) {
  if (value === null) return "—"
  return value.kind === "translation" ? t(value.key) : value.value
}

function useChartColors() {
  const [colors, setColors] = useState({
    line: "",
    point: "",
    text: "",
    grid: "",
  })

  useEffect(() => {
    const updateColors = () => {
      const styles = getComputedStyle(document.documentElement)
      setColors({
        line: styles.getPropertyValue("--lagoon-deep").trim(),
        point: styles.getPropertyValue("--lagoon").trim(),
        text: styles.getPropertyValue("--sea-ink-soft").trim(),
        grid: styles.getPropertyValue("--line").trim(),
      })
    }

    updateColors()
    const observer = new MutationObserver(updateColors)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    })
    return () => observer.disconnect()
  }, [])

  return colors
}

export function ReportList({
  locale,
  reports,
}: {
  locale: Locale
  reports: ReadonlyArray<ProvisionalReport>
}) {
  const { t } = useTranslation()
  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("reportsEyebrow")}</p>
        <h1 className="my-3 text-[clamp(2.2rem,6vw,3.8rem)] leading-none font-extrabold tracking-[-0.055em]">
          {t("reportsTitle")}
        </h1>
        <p className="leading-7 text-sea-ink-soft">{t("reportsDescription")}</p>
        <p className="mt-4 inline-flex rounded-md border border-lagoon/35 bg-lagoon/10 px-3 py-2 text-sm font-bold text-sea-ink">
          {t("reportMockNotice")}
        </p>
      </header>
      {reports.length === 0 ? (
        <section className="rounded-xl border border-dashed border-line bg-surface p-[clamp(2rem,6vw,4rem)] text-center">
          <h2 className="mt-0">{t("reportsEmptyTitle")}</h2>
          <p className="mb-0 text-sea-ink-soft">
            {t("reportsEmptyDescription")}
          </p>
        </section>
      ) : (
        <section
          className="grid border-y border-line md:grid-cols-2"
          aria-label={t("reportsTitle")}
        >
          {reports.map(report => (
            <article
              key={report.marketCode}
              className="min-w-0 border-b border-line p-5 last:border-b-0 md:[&:nth-child(odd)]:border-r md:[&:nth-last-child(-n+2)]:border-b-0"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="m-0 text-xl tracking-[-0.025em]">
                    {t(`reportMarket_${report.marketCode}`)}
                  </h2>
                  <p className="mt-2 mb-0 text-sm text-sea-ink-soft">
                    {t(`reportMarketDescription_${report.marketCode}`)}
                  </p>
                </div>
                <StatusBadge status={report.status} />
              </div>
              <dl className="mt-5 grid grid-cols-2 gap-3 text-sm">
                <div>
                  <dt className="text-sea-ink-soft">
                    {t("reportEditionDate")}
                  </dt>
                  <dd className="m-0 font-bold">{report.editionDate}</dd>
                </div>
                <div>
                  <dt className="text-sea-ink-soft">{t("reportSourceDate")}</dt>
                  <dd className="m-0 font-bold">{report.sourceDate ?? "—"}</dd>
                </div>
              </dl>
              <Link
                to="/$locale/reports/$marketCode"
                params={{ locale, marketCode: report.marketCode }}
                className="mt-5 inline-flex rounded-lg border border-chip-line bg-chip px-3 py-2 text-sm font-extrabold text-sea-ink no-underline transition hover:bg-surface-strong"
              >
                {t("reportViewDetails")}
              </Link>
            </article>
          ))}
        </section>
      )}
    </main>
  )
}

export function ReportDetail({
  locale,
  report,
}: {
  locale: Locale
  report: ProvisionalReport
}) {
  const { t } = useTranslation()
  return (
    <main className="page-shell">
      <Link
        to="/$locale/reports"
        params={{ locale }}
        className="text-sm font-bold text-lagoon-deep"
      >
        {t("reportBack")}
      </Link>
      <header className="mt-5 mb-8">
        <p className="eyebrow">{t("reportsEyebrow")}</p>
        <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="m-0 text-[clamp(2rem,5vw,3.25rem)] leading-none font-extrabold tracking-[-0.05em]">
              {t(`reportMarket_${report.marketCode}`)}
            </h1>
            <p className="mt-3 max-w-3xl leading-7 text-sea-ink-soft">
              {t(`reportMarketDescription_${report.marketCode}`)}
            </p>
          </div>
          <StatusBadge status={report.status} />
        </div>
        <dl className="mt-5 flex flex-wrap gap-x-8 gap-y-3 text-sm">
          <div>
            <dt className="text-sea-ink-soft">{t("reportEditionDate")}</dt>
            <dd className="m-0 font-bold">{report.editionDate}</dd>
          </div>
          <div>
            <dt className="text-sea-ink-soft">{t("reportSourceDate")}</dt>
            <dd className="m-0 font-bold">{report.sourceDate ?? "—"}</dd>
          </div>
        </dl>
      </header>
      <section
        className={`mb-6 border-l-4 p-4 ${statusStyles[report.status]}`}
        aria-label={t("reportStatus")}
      >
        {t(`reportStatusDescription_${report.status}`)}
      </section>
      <aside className="mb-8 rounded-lg border border-line bg-surface p-4 text-sm text-sea-ink-soft">
        {t(report.caveatKey)}
      </aside>
      <div className="grid min-w-0 gap-5">
        {report.blocks.map((block, index) => (
          <ReportBlockView block={block} key={`${block.titleKey}-${index}`} />
        ))}
      </div>
    </main>
  )
}

function ReportBlockView({ block }: { block: ReportBlock }) {
  const { t } = useTranslation()
  const chartColors = useChartColors()
  return (
    <section className="min-w-0 border-t border-line pt-5">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="m-0 text-xl tracking-[-0.02em]">{t(block.titleKey)}</h2>
        <BlockStatusBadge status={block.status} />
      </div>
      {block.kind === "metric" ? (
        <div className="grid min-w-0 gap-px overflow-hidden rounded-xl border border-line sm:grid-cols-2 lg:grid-cols-3">
          {block.metrics.map(item => (
            <div className="min-w-0 bg-surface p-4" key={item.labelKey}>
              <p className="m-0 text-sm text-sea-ink-soft">
                {t(item.labelKey)}
              </p>
              <p className="mt-2 mb-0 text-2xl font-extrabold">
                {reportValue(item.value, t)}
              </p>
              {"change" in item ? (
                <p className="mt-1 mb-0 text-sm font-bold text-lagoon-deep">
                  {reportValue(item.change, t)}
                </p>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {block.kind === "table" ? (
        <div className="min-w-0 max-w-full overflow-x-auto rounded-xl border border-line">
          <table className="w-max min-w-full text-left text-sm">
            <thead className="bg-link-hover">
              <tr>
                {block.columns.map(column => (
                  <th
                    className="whitespace-nowrap px-4 py-3 font-extrabold"
                    key={column}
                  >
                    {t(column)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, index) => (
                <tr className="border-t border-line" key={index}>
                  {row.map((cell, cellIndex) => (
                    <td className="whitespace-nowrap px-4 py-3" key={cellIndex}>
                      {reportValue(cell, t)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {block.kind === "series" ? (
        <>
          <div className="h-64 min-w-0 w-full rounded-xl border border-line bg-surface p-2">
            <ClientOnly
              fallback={
                <div
                  className="h-full w-full animate-pulse rounded-lg bg-surface-strong"
                  role="status"
                  aria-label={t("reportChartSummary")}
                />
              }
            >
              <ReactECharts
                style={{ height: "100%", width: "100%" }}
                option={{
                  animation: false,
                  grid: { left: 42, right: 16, top: 18, bottom: 28 },
                  xAxis: {
                    type: "category",
                    data: block.points.map(point =>
                      reportValue(point.label, t)
                    ),
                    axisLabel: { color: chartColors.text },
                    axisLine: { lineStyle: { color: chartColors.grid } },
                  },
                  yAxis: {
                    type: "value",
                    scale: true,
                    axisLabel: { color: chartColors.text },
                    splitLine: { lineStyle: { color: chartColors.grid } },
                  },
                  series: [
                    {
                      type: "line",
                      data: block.points.map(point => point.value),
                      connectNulls: false,
                      symbolSize: 7,
                      lineStyle: { color: chartColors.line },
                      itemStyle: { color: chartColors.point },
                    },
                  ],
                }}
              />
            </ClientOnly>
          </div>
          <div className="sr-only">
            <table>
              <caption>{t("reportChartSummary")}</caption>
              <tbody>
                {block.points.map(point => (
                  <tr key={reportValue(point.label, t)}>
                    <th>{reportValue(point.label, t)}</th>
                    <td>{point.value ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  )
}

export function ReportErrorScreen({ error }: { error: Error }) {
  void error
  const { t } = useTranslation()
  const router = useRouter()
  return (
    <main className="page-shell">
      <section
        className="rounded-xl border border-red-500/35 bg-red-500/10 p-[clamp(2rem,6vw,4rem)] text-center"
        role="alert"
      >
        <h1 className="mt-0 text-2xl">{t("reportsErrorTitle")}</h1>
        <p className="mx-auto max-w-xl text-sea-ink-soft">
          {t("reportsErrorDescription")}
        </p>
        <button type="button" onClick={() => void router.invalidate()}>
          {t("retry")}
        </button>
      </section>
    </main>
  )
}
