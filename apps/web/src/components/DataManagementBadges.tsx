import {
  CircleCheck,
  CircleHelp,
  CircleMinus,
  CircleX,
  Clock,
  LoaderCircle,
  PauseCircle,
  RefreshCw,
  TriangleAlert,
  Upload,
} from "lucide-react"
import { useTranslation } from "react-i18next"

const statuses = {
  pending: { icon: Clock, key: "dataManagementStatusPending" },
  running: { icon: LoaderCircle, key: "dataManagementStatusRunning" },
  retry_wait: { icon: RefreshCw, key: "dataManagementStatusRetryWait" },
  succeeded: { icon: CircleCheck, key: "dataManagementStatusSucceeded" },
  no_change: { icon: CircleMinus, key: "dataManagementStatusNoChange" },
  partial: { icon: TriangleAlert, key: "dataManagementStatusPartial" },
  unavailable: { icon: PauseCircle, key: "dataManagementStatusUnavailable" },
  failed: { icon: CircleX, key: "dataManagementStatusFailed" },
  cancelled: { icon: CircleMinus, key: "dataManagementStatusCancelled" },
} as const

export function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation()
  const definition = Object.hasOwn(statuses, status)
    ? statuses[status as keyof typeof statuses]
    : null
  const Icon = definition?.icon ?? CircleHelp
  return (
    <span className="inline-flex items-center gap-1 rounded bg-link-hover px-2 py-0.5 text-xs font-semibold text-sea-ink">
      <Icon aria-hidden="true" className="size-3.5 shrink-0" />
      {definition
        ? t(definition.key)
        : t("dataManagementStatusUnknown", { status })}
    </span>
  )
}

export function JobKindBadge({
  kind,
  jobKey,
}: {
  kind: "function" | "projection"
  jobKey?: string
}) {
  const { t } = useTranslation()
  const publish = kind === "projection" || jobKey === "news_publish_job"
  const Icon = publish ? Upload : RefreshCw
  return (
    <span
      className={
        publish
          ? "inline-flex items-center gap-1 rounded border border-lagoon bg-lagoon-tint px-2 py-0.5 text-xs font-semibold text-sea-ink"
          : "inline-flex items-center gap-1 rounded border border-line px-2 py-0.5 text-xs font-semibold text-sea-ink-soft"
      }
    >
      <Icon aria-hidden="true" className="size-3.5 shrink-0" />
      {t(publish ? "dataManagementKindPublish" : "dataManagementKindRefresh")}
    </span>
  )
}
