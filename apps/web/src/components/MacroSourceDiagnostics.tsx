import { useTranslation } from "react-i18next"
import { z } from "zod"

const sourcesSchema = z.array(
  z.object({
    code: z.string(),
    name: z.string(),
    status: z.enum(["ok", "degraded", "unavailable", "disabled"]),
    fetched_at: z.iso.datetime({ offset: true }),
    affected_items: z.array(z.string()),
    failures: z.array(
      z.object({
        affected_items: z.array(z.string()),
        endpoint: z.string(),
        failure_type: z.string(),
        http_status: z.number().int().nullable(),
      })
    ),
  })
)

export function MacroSourceDiagnostics({
  result,
  error,
}: {
  result: Record<string, unknown> | null
  error: string | null
}) {
  const { t, i18n } = useTranslation()
  const parsed = sourcesSchema.safeParse(result?.sources)
  const labels = {
    ok: t("macroSourceOk"),
    degraded: t("macroSourceDegraded"),
    unavailable: t("macroSourceUnavailable"),
    disabled: t("macroSourceDisabled"),
  }
  const failureLabels: Record<string, string> = {
    timeout: t("macroFailureTimeout"),
    dns_error: t("macroFailureDns"),
    connection_error: t("macroFailureConnection"),
    rate_limited: t("macroFailureRate"),
    authentication_error: t("macroFailureAuth"),
    http_error: t("macroFailureHttp"),
    parse_error: t("macroFailureParse"),
    validation_error: t("macroFailureValidation"),
    empty_response: t("macroFailureEmpty"),
    unknown: t("macroFailureUnknown"),
  }
  return (
    <div className="mt-3 text-sm text-sea-ink-soft">
      <h3 className="font-bold text-sea-ink">{t("macroSourceTitle")}</h3>
      {error ? (
        <p>
          {error === "macro_sources_unavailable"
            ? t("macroMarketFailure")
            : error}{" "}
          · <code>{error}</code>
        </p>
      ) : null}
      {!parsed.success || parsed.data.length === 0 ? (
        <p>{t("macroSourcesNotRecorded")}</p>
      ) : (
        <ul className="mt-2 grid gap-3">
          {parsed.data.map(source => (
            <li key={source.code} className="rounded-md border border-line p-3">
              <p className="font-bold text-sea-ink">
                {source.name} · {labels[source.status]}
              </p>
              <p>
                {t("macroSourceFetched", {
                  time: new Intl.DateTimeFormat(i18n.language, {
                    dateStyle: "short",
                    timeStyle: "medium",
                  }).format(new Date(source.fetched_at)),
                })}
              </p>
              {source.affected_items.length > 0 ? (
                <p>
                  {t("macroSourceAffected", {
                    items: source.affected_items.join(", "),
                  })}
                </p>
              ) : null}
              {source.failures.length > 0 ? (
                <ul className="mt-1 list-disc pl-5">
                  {source.failures.map((failure, index) => (
                    <li key={`${failure.endpoint}-${index}`}>
                      {failure.endpoint} ·{" "}
                      {failureLabels[failure.failure_type] ??
                        t("macroFailureUnknown")}
                      {failure.http_status !== null
                        ? ` · HTTP ${failure.http_status}`
                        : ""}
                      {failure.affected_items.length > 0
                        ? ` · ${failure.affected_items.join(", ")}`
                        : ""}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
