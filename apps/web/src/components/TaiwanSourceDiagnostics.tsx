import { useTranslation } from "react-i18next"
import { z } from "zod"

const daySchema = z.object({
  trade_date: z.iso.date(),
  status: z.enum(["stored", "existing", "no_data", "failed"]),
  record_count: z.number().int().nonnegative().optional(),
  fetched_at: z.iso.datetime({ offset: true }).optional(),
  error: z.string().optional(),
})
const walkSchema = z.object({
  covered_trading_days: z.number().int().nonnegative(),
  lookback_trading_days: z.number().int().positive(),
  aborted: z.boolean(),
  days: z.array(daySchema),
})
const indexSchema = z.object({
  symbol: z.string(),
  status: z.enum(["succeeded", "partial", "failed"]),
  record_count: z.number().int().nonnegative().optional(),
  fetched_at: z.iso.datetime({ offset: true }).optional(),
  source_as_of: z.iso.date().optional(),
  error: z.string().optional(),
})

export function TaiwanSourceDiagnostics({
  result,
  error,
}: {
  result: Record<string, unknown> | null
  error: string | null
}) {
  const { t } = useTranslation()
  const labels = {
    stored: t("twSourceStored"),
    existing: t("twSourceExisting"),
    no_data: t("twSourceNoData"),
    failed: t("macroSourceUnavailable"),
  }
  const parsedIndex = indexSchema.safeParse(result?.index)
  const index = parsedIndex.success ? parsedIndex.data : null
  const indexStatus =
    error === "twse_unavailable"
      ? t("macroSourceDisabled")
      : !index
        ? t("macroSourcesNotRecorded")
        : index.status === "succeeded"
          ? t("macroSourceOk")
          : index.status === "partial"
            ? t("macroSourceDegraded")
            : t("macroSourceUnavailable")
  return (
    <div className="mt-3 text-sm text-sea-ink-soft">
      <h3 className="font-bold text-sea-ink">{t("macroSourceTitle")}</h3>
      {error ? <p role="status">{error}</p> : null}
      <ul className="grid gap-3">
        <li className="rounded-md border border-line p-3">
          <p className="font-bold text-sea-ink">
            TWSE · {index?.symbol ?? "^TWII"} · {t("twSourceIndex")} ·{" "}
            {indexStatus}
          </p>
          <code>
            /en/indicesReport/MI_5MINS_HIST · /en/exchangeReport/FMTQIK
          </code>
          {index ? (
            <p>
              {`${t("twSourceRows")}: ${index.record_count ?? "—"} · ${t("twSourceFetched")}: ${index.fetched_at ?? "—"} · source_as_of: ${index.source_as_of ?? "—"}${index.error ? ` · ${t("twSourceError")}: ${index.error}` : ""}`}
            </p>
          ) : null}
        </li>
        {(
          [
            ["market_flows", "BFI82U", t("twSourceMarket")],
            ["stock_flows", "T86", t("twSourceStock")],
          ] as const
        ).map(([key, endpoint, name]) => {
          const parsed = walkSchema.safeParse(result?.[key])
          const walk = parsed.success ? parsed.data : null
          const failed = walk?.days.some(day => day.status === "failed")
          const status =
            error === "twse_unavailable"
              ? t("macroSourceDisabled")
              : !walk
                ? t("macroSourcesNotRecorded")
                : failed || walk.aborted
                  ? t(
                      walk.covered_trading_days
                        ? "macroSourceDegraded"
                        : "macroSourceUnavailable"
                    )
                  : !walk.covered_trading_days
                    ? t("twSourceNoData")
                    : walk.covered_trading_days < walk.lookback_trading_days
                      ? t("twSourceIncomplete")
                      : t("macroSourceOk")
          return (
            <li key={key} className="rounded-md border border-line p-3">
              <p className="font-bold text-sea-ink">
                TWSE · {endpoint} · {name} · {status}
              </p>
              <code>/rwd/zh/fund/{endpoint}</code>
              {walk ? (
                <>
                  <p>
                    {t("twSourceCoverage", {
                      count: walk.covered_trading_days,
                      target: walk.lookback_trading_days,
                    })}
                  </p>
                  <details className="mt-2">
                    <summary className="cursor-pointer">
                      {t("twSourceDays")}
                    </summary>
                    <div className="mt-2 overflow-x-auto">
                      <table className="w-full text-left">
                        <thead>
                          <tr>
                            {[
                              t("twSourceDate"),
                              t("twSourceStatus"),
                              t("twSourceRows"),
                              t("twSourceFetched"),
                              t("twSourceError"),
                            ].map(label => (
                              <th key={label} className="p-2">
                                {label}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {walk.days.map(day => (
                            <tr
                              key={day.trade_date}
                              className="border-t border-line"
                            >
                              <td className="p-2 whitespace-nowrap">
                                {day.trade_date}
                              </td>
                              <td className="p-2">{labels[day.status]}</td>
                              <td className="p-2">{day.record_count ?? "—"}</td>
                              <td className="p-2">{day.fetched_at ?? "—"}</td>
                              <td className="p-2">{day.error ?? "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </details>
                </>
              ) : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
