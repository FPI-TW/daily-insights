import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"

export function MarketQuerySection<T>({
  query,
  loading,
  children,
}: {
  query: {
    data: T | undefined
    isPending: boolean
    isFetching: boolean
    error: Error | null
    refetch: () => unknown
  }
  loading: ReactNode
  children: (data: T | undefined) => ReactNode
}) {
  const { t } = useTranslation()
  return (
    <>
      {query.data === undefined && query.isPending
        ? loading
        : children(query.data)}
      {query.isFetching && query.data !== undefined ? (
        <p
          role="status"
          aria-live="polite"
          className="my-2 text-xs text-sea-ink-soft"
        >
          {t("reportLoadingAnnouncement")}
        </p>
      ) : null}
      {query.error ? (
        <div
          role="alert"
          className="my-3 flex items-center gap-3 text-sm text-sea-ink-soft"
        >
          <span>{t("unexpectedError")}</span>
          <button
            type="button"
            className="font-bold text-link underline"
            disabled={query.isFetching}
            onClick={() => {
              void query.refetch()
            }}
          >
            {t("retry")}
          </button>
        </div>
      ) : null}
    </>
  )
}
