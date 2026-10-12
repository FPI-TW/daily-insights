import type { Locale, SectorHeatmap } from "@daily-insights/api-client"
import { useTranslation } from "react-i18next"
import { formatIsoDate } from "#/lib/format"

export function HkSectorHeatmapLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel mt-4 p-5"
      role="status"
      aria-live="polite"
      aria-label={t("hkSectorHeatmapLoading")}
    >
      <div className="h-3 w-32 animate-pulse rounded bg-line" />
      <div className="mt-4 aspect-[2/1] w-full animate-pulse rounded bg-line" />
    </section>
  )
}

export function HkSectorHeatmap({
  heatmap,
  locale,
}: {
  heatmap: SectorHeatmap | null
  locale: Locale
}) {
  const { t } = useTranslation()
  if (heatmap === null) {
    return (
      <section
        className="surface-panel mt-4 p-5"
        role="status"
        aria-live="polite"
      >
        <p className="m-0 text-sm text-sea-ink-soft">
          {t("hkSectorHeatmapUnavailable")}
        </p>
      </section>
    )
  }
  const overview = heatmap.images.find(
    image => image.kind === "overview" && image.slug === "overview"
  )
  // The capture splits the overview at a sector gutter so the crowded right
  // side gets a full row of its own; the whole image stays the link target.
  const overviewParts = heatmap.images.filter(
    image => image.kind === "overview" && image.slug !== "overview"
  )
  const overviewRows = overviewParts.length
    ? overviewParts
    : overview
      ? [overview]
      : []
  const legend = heatmap.images.find(image => image.kind === "legend")
  return (
    <section
      className="surface-panel mt-4 p-5"
      aria-label={t("hkSectorHeatmapTitle")}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="eyebrow m-0">{t("hkSectorHeatmapTitle")}</p>
        <p className="m-0 font-mono text-xs text-sea-ink-soft">
          {t("hkSectorHeatmapEdition", {
            date: formatIsoDate(heatmap.edition_date, locale),
          })}
        </p>
      </div>
      {overviewRows.map((image, index) => (
        // The tiles' labels are drawn at 1920px, so each half takes the full
        // content width and the whole overview stays a click away.
        <a
          key={image.slug}
          className="mt-4 block"
          href={(overview ?? image).url}
          target="_blank"
          rel="noreferrer"
          title={t("hkSectorHeatmapOpenFullSize")}
        >
          <img
            className="h-auto w-full rounded"
            src={image.url}
            width={image.width}
            height={image.height}
            alt={
              overviewParts.length
                ? t("hkSectorHeatmapOverviewPartAlt", { index: index + 1 })
                : t("hkSectorHeatmapOverviewAlt")
            }
          />
        </a>
      ))}
      {legend ? (
        <img
          className="mt-3 h-auto max-w-full"
          src={legend.url}
          width={legend.width}
          height={legend.height}
          alt={t("hkSectorHeatmapLegendAlt")}
        />
      ) : null}
    </section>
  )
}
