import { useTranslation } from "react-i18next"
import type { TokenUsage } from "#/lib/admin-chat"

export function TokenUsageTotal({ usage }: { usage: TokenUsage }) {
  const { t, i18n } = useTranslation()
  return (
    <span className="tabular-nums">
      {usage.total_tokens === null
        ? t("tokenUnavailable")
        : usage.total_tokens.toLocaleString(i18n.language)}
      {usage.completeness === "partial" && (
        <span className="ml-2 text-xs text-sea-ink-soft">
          {t("tokenKnown")}
        </span>
      )}
    </span>
  )
}

export function TokenUsageDetails({ usage }: { usage: TokenUsage }) {
  const { t, i18n } = useTranslation()
  const format = (value: number | null) =>
    value === null ? t("tokenUnavailable") : value.toLocaleString(i18n.language)
  return (
    <div>
      <dl className="my-2 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <dt className="text-sm text-sea-ink-soft">{t("tokenInput")}</dt>
          <dd className="m-0 font-bold tabular-nums">
            {format(usage.input_tokens)}
          </dd>
        </div>
        <div>
          <dt className="text-sm text-sea-ink-soft">{t("tokenOutput")}</dt>
          <dd className="m-0 font-bold tabular-nums">
            {format(usage.output_tokens)}
          </dd>
        </div>
        <div>
          <dt className="text-sm text-sea-ink-soft">{t("tokenTotal")}</dt>
          <dd className="m-0 font-bold">
            <TokenUsageTotal usage={usage} />
          </dd>
        </div>
      </dl>
      {usage.completeness === "partial" && (
        <p className="m-0 text-xs text-sea-ink-soft">
          {t("tokenPartialNotice")}
        </p>
      )}
    </div>
  )
}
