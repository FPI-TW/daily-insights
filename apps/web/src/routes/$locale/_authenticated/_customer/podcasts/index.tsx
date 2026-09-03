import { createFileRoute } from "@tanstack/react-router"
import { motion } from "motion/react"
import { useTranslation } from "react-i18next"
import { PodcastPlayer } from "#/components/PodcastPlayer"
import { ErrorScreen, LoadingScreen } from "#/components/StateScreen"
import { hoverLift, reveal, springs, useEnterAnimation } from "#/lib/motion"
import { getPodcastList } from "#/lib/podcasts"
import { numberLocales } from "#/lib/format"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/podcasts/"
)({
  loader: ({ context }) => getPodcastList({ data: context.locale }),
  pendingComponent: LoadingScreen,
  errorComponent: ErrorScreen,
  component: PodcastListPage,
})

function PodcastListPage() {
  const episodes = Route.useLoaderData()
  const { locale, user } = Route.useRouteContext()
  const { t } = useTranslation()
  const animate = useEnterAnimation()

  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("podcastEyebrow")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("podcastTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">{t("podcastDescription")}</p>
      </header>
      {episodes.length === 0 ? (
        <section className="rounded-xl border border-dashed border-line bg-surface p-[clamp(2rem,6vw,4rem)] text-center">
          <h2 className="mt-0">{t("podcastEmptyTitle")}</h2>
          <p className="mb-0 text-sea-ink-soft">
            {t("podcastEmptyDescription")}
          </p>
        </section>
      ) : (
        <ol className="m-0 grid list-none gap-4 p-0">
          {episodes.map((episode, index) => (
            <motion.li
              key={episode.id}
              className="surface-panel border-l-[3px] border-l-transparent p-5 text-sea-ink transition-[border-color,box-shadow] hover:border-l-lagoon hover:border-lagoon-deep hover:shadow-[0_16px_34px_rgb(14_20_19/9%)] max-[42rem]:p-4"
              {...reveal(animate, index)}
              whileHover={hoverLift}
              transition={springs.snappy}
            >
              <article className="min-w-0">
                {/* The trading date appears once, as the eyebrow; the title is
                    the episode's own title rather than a date composite. */}
                <p className="m-0 flex flex-wrap items-center gap-2 text-xs font-extrabold tracking-[0.06em] text-kicker">
                  <time dateTime={episode.trading_date}>
                    {new Intl.DateTimeFormat(numberLocales[locale], {
                      dateStyle: "long",
                      timeZone: "UTC",
                    }).format(new Date(`${episode.trading_date}T00:00:00Z`))}
                  </time>
                  {episode.duration_seconds ? (
                    <span className="font-bold text-sea-ink-soft">
                      {t("podcastDuration", {
                        minutes: Math.max(
                          1,
                          Math.round(episode.duration_seconds / 60)
                        ),
                      })}
                    </span>
                  ) : null}
                </p>
                <h2 className="mt-1 mb-0 text-[clamp(1.05rem,3vw,1.35rem)] tracking-[-0.02em]">
                  {episode.title}
                </h2>
                <p className="mt-2 mb-0 line-clamp-3 text-sm leading-6 text-sea-ink-soft">
                  {episode.summary}
                </p>
                <PodcastPlayer
                  key={`${episode.id}:${locale}`}
                  episodeId={episode.id}
                  locale={locale}
                  user={user}
                  title={episode.title}
                />
              </article>
            </motion.li>
          ))}
        </ol>
      )}
    </main>
  )
}
