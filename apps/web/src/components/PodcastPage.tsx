import type {
  Locale,
  PodcastAudioPlayback,
  PodcastEpisodeSummary,
  User,
} from "@daily-insights/api-client"
import { Link } from "@tanstack/react-router"
import {
  type MouseEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import { useTranslation } from "react-i18next"
import {
  chapterSegments,
  clampPosition,
  clockTime,
  collapsedListSize,
  currentChapterIndex,
  dotDate,
  episodeFilters,
  formatSpeed,
  listeningStatus,
  matchesFilter,
  monthDayLabel,
  nextPlaybackSpeed,
  releaseTimeLabel,
  skipSeconds,
  waveformHeights,
  weekdayLabel,
  type EpisodeFilter,
  type ListeningStatus,
  type PlaybackSpeed,
} from "#/lib/podcast-episode"
import {
  readPodcastProgress,
  writePodcastProgress,
} from "#/lib/podcast-progress"
import { browserPodcastClient } from "#/lib/podcasts"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

// Design parameters from the handoff; the product defaults are the
// prototype's defaults.
export type PodcastPageOptions = {
  progressStyle: "waveform" | "line"
  showChapters: boolean
  autoplayNext: boolean
  showListSummary: boolean
}

export const defaultPodcastPageOptions: PodcastPageOptions = {
  progressStyle: "waveform",
  showChapters: true,
  autoplayNext: false,
  showListSummary: true,
}

type ProgressMap = Record<
  string,
  { positionSeconds: number; durationSeconds: number }
>

type PendingAction = { play: boolean; seekTo: number | null }

const shareResetMs = 1600
const progressSaveIntervalSeconds = 5

const chipClass =
  "rounded-lg border border-chip-line bg-surface-strong px-2.5 py-1.5 text-[13px] leading-normal text-sea-ink-soft no-underline transition-colors hover:border-lagoon-deep hover:text-lagoon-deep"

function isInteractiveTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false
  return target.closest("button, a, input, textarea, select, [role=slider]")
}

export function PodcastPage({
  episodes,
  locale,
  user,
  initialEpisodeId,
  options = defaultPodcastPageOptions,
}: {
  episodes: PodcastEpisodeSummary[]
  locale: Locale
  user: User
  initialEpisodeId?: string | undefined
  options?: PodcastPageOptions
}) {
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "customer")
  const latest = episodes[0]
  const [currentId, setCurrentId] = useState(() =>
    episodes.some(episode => episode.id === initialEpisodeId)
      ? (initialEpisodeId as string)
      : (latest?.id ?? "")
  )
  const current = episodes.find(episode => episode.id === currentId) ?? latest

  const audioRef = useRef<HTMLAudioElement>(null)
  const downloadRef = useRef<HTMLAnchorElement>(null)
  const requestGeneration = useRef(0)
  const lastSavedAt = useRef(0)
  const pendingAction = useRef<PendingAction | null>(null)
  const downloadPending = useRef(false)
  const shareTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const [playback, setPlayback] = useState<PodcastAudioPlayback | null>(null)
  const [loading, setLoading] = useState(false)
  const [unavailable, setUnavailable] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [mediaDuration, setMediaDuration] = useState<number | null>(null)
  const [speed, setSpeed] = useState<PlaybackSpeed>(1)
  const [filter, setFilter] = useState<EpisodeFilter>("all")
  const [showAll, setShowAll] = useState(false)
  const [progress, setProgress] = useState<ProgressMap>({})
  const [shared, setShared] = useState(false)

  const duration =
    mediaDuration ??
    current?.duration_seconds ??
    progress[currentId]?.durationSeconds ??
    0

  // Stored progress is per browser, so it is read after hydration and the
  // current episode resumes where the listener left it (finished → start).
  useEffect(() => {
    const restored: ProgressMap = {}
    for (const episode of episodes) {
      const stored = readPodcastProgress(user.id, episode.id, locale)
      if (stored) restored[episode.id] = stored
    }
    setProgress(restored)
    const own = restored[currentId]
    if (own) {
      const status = listeningStatus(
        own.positionSeconds,
        own.durationSeconds || null
      )
      setPosition(status.kind === "finished" ? 0 : own.positionSeconds)
    }
    // Only the first mount restores; later episode switches restore inline.
  }, [])

  const rememberProgress = useCallback(
    (
      episodeId: string,
      resolvedLocale: Locale,
      positionSeconds: number,
      durationSeconds: number
    ) => {
      if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return
      writePodcastProgress(user.id, episodeId, resolvedLocale, {
        positionSeconds,
        durationSeconds,
      })
      setProgress(previous => ({
        ...previous,
        [episodeId]: { positionSeconds, durationSeconds },
      }))
    },
    [user.id]
  )

  const saveCurrentProgress = useCallback(() => {
    const audio = audioRef.current
    if (!audio || !playback) return
    rememberProgress(
      playback.episode_id,
      playback.resolved_locale,
      audio.currentTime,
      audio.duration
    )
  }, [playback, rememberProgress])

  useEffect(() => {
    const saveBeforeUnload = () => saveCurrentProgress()
    window.addEventListener("beforeunload", saveBeforeUnload)
    return () => {
      saveCurrentProgress()
      window.removeEventListener("beforeunload", saveBeforeUnload)
    }
  }, [saveCurrentProgress])

  useLayoutEffect(() => {
    return () => {
      requestGeneration.current += 1
      if (shareTimer.current) clearTimeout(shareTimer.current)
    }
  }, [])

  const loadAudio = useCallback(
    async (episodeId: string, action: PendingAction) => {
      const generation = ++requestGeneration.current
      pendingAction.current = action
      setLoading(true)
      setUnavailable(false)
      setPlayback(null)
      setMediaDuration(null)
      try {
        const nextPlayback = await browserPodcastClient().createAudioUrl(
          episodeId,
          locale
        )
        if (generation !== requestGeneration.current) return
        setPlayback(nextPlayback)
      } catch (caught) {
        if (generation !== requestGeneration.current) return
        if (await redirectExpiredSession(caught)) return
        pendingAction.current = null
        setUnavailable(true)
      } finally {
        if (generation === requestGeneration.current) setLoading(false)
      }
    },
    [locale, redirectExpiredSession]
  )

  const play = useCallback(() => {
    const audio = audioRef.current
    if (!audio || !playback) {
      if (current && !loading) {
        void loadAudio(current.id, { play: true, seekTo: null })
      } else if (pendingAction.current) {
        pendingAction.current.play = true
      }
      return
    }
    audio.playbackRate = speed
    void audio.play().catch(() => {
      // Autoplay policy: the listener presses play again.
      setPlaying(false)
    })
  }, [current, loadAudio, loading, playback, speed])

  const pause = useCallback(() => {
    audioRef.current?.pause()
  }, [])

  const seek = useCallback(
    (seconds: number, { andPlay = false } = {}) => {
      const target = clampPosition(seconds, duration)
      const audio = audioRef.current
      if (audio && playback) {
        audio.currentTime = target
        setPosition(target)
        if (andPlay) play()
        return
      }
      setPosition(target)
      if (current && !loading) {
        void loadAudio(current.id, { play: andPlay, seekTo: target })
      } else if (pendingAction.current) {
        pendingAction.current.seekTo = target
        if (andPlay) pendingAction.current.play = true
      }
    },
    [current, duration, loadAudio, loading, play, playback]
  )

  const togglePlay = useCallback(() => {
    if (playing) pause()
    else play()
  }, [pause, play, playing])

  const selectEpisode = useCallback(
    (episodeId: string) => {
      if (episodeId === currentId) {
        togglePlay()
        return
      }
      saveCurrentProgress()
      audioRef.current?.pause()
      setPlaying(false)
      setCurrentId(episodeId)
      lastSavedAt.current = 0
      const stored = progress[episodeId]
      const status = stored
        ? listeningStatus(
            stored.positionSeconds,
            stored.durationSeconds || null
          )
        : null
      const resumeAt =
        stored && status && status.kind !== "finished"
          ? stored.positionSeconds
          : 0
      setPosition(resumeAt)
      void loadAudio(episodeId, { play: true, seekTo: resumeAt })
    },
    [currentId, loadAudio, progress, saveCurrentProgress, togglePlay]
  )

  const playNext = useCallback(() => {
    const index = episodes.findIndex(episode => episode.id === currentId)
    const next = episodes[index + 1]
    if (next) selectEpisode(next.id)
  }, [currentId, episodes, selectEpisode])

  // Space toggles playback and the arrow keys skip, unless focus sits on a
  // control that owns those keys.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || isInteractiveTarget(event.target)) return
      if (event.key === " ") {
        event.preventDefault()
        togglePlay()
      } else if (event.key === "ArrowLeft") {
        seek(position - skipSeconds)
      } else if (event.key === "ArrowRight") {
        seek(position + skipSeconds)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [position, seek, togglePlay])

  // A download requested before the audio URL existed fires once it does.
  useEffect(() => {
    if (playback && downloadPending.current) {
      downloadPending.current = false
      downloadRef.current?.click()
    }
  }, [playback])

  function cycleSpeed() {
    const next = nextPlaybackSpeed(speed)
    setSpeed(next)
    if (audioRef.current) audioRef.current.playbackRate = next
  }

  async function share() {
    if (!current) return
    const url = new URL(`/${locale}/podcasts`, window.location.origin)
    url.searchParams.set("episode", current.id)
    try {
      await navigator.clipboard.writeText(url.toString())
    } catch {
      // Clipboard access can be denied; the label still confirms the intent.
    }
    setShared(true)
    if (shareTimer.current) clearTimeout(shareTimer.current)
    shareTimer.current = setTimeout(() => setShared(false), shareResetMs)
  }

  function requestDownload(event: MouseEvent<HTMLAnchorElement>) {
    if (playback || !current) return
    event.preventDefault()
    downloadPending.current = true
    if (!loading) void loadAudio(current.id, { play: false, seekTo: null })
  }

  function seekFromPointer(event: MouseEvent<HTMLElement>) {
    const bounds = event.currentTarget.getBoundingClientRect()
    if (bounds.width <= 0 || duration <= 0) return
    const ratio = (event.clientX - bounds.left) / bounds.width
    seek(ratio * duration)
  }

  function durationLabel(seconds: number) {
    const minutes = Math.floor(seconds / 60)
    const rest = Math.floor(seconds % 60)
    return rest
      ? t("podcastMinutesSeconds", {
          minutes,
          seconds: String(rest).padStart(2, "0"),
        })
      : t("podcastMinutesOnly", { minutes })
  }

  function statusLabel(status: ListeningStatus) {
    if (status.kind === "finished") return t("podcastStatusFinished")
    if (status.kind === "partial")
      return t("podcastStatusPartial", { percent: status.percent })
    return t("podcastStatusUnheard")
  }

  const bars = useMemo(
    () => waveformHeights(duration, options.progressStyle),
    [duration, options.progressStyle]
  )
  const playedFraction = duration > 0 ? position / duration : 0
  const segments = useMemo(
    () => (current ? chapterSegments(current.chapters, duration) : []),
    [current, duration]
  )
  const activeChapter = currentChapterIndex(segments, position)

  const others = episodes.filter(episode => episode.id !== currentId)
  const filtered = others.filter(episode => {
    const stored = progress[episode.id]
    return matchesFilter(
      filter,
      listeningStatus(
        stored?.positionSeconds ?? 0,
        stored?.durationSeconds || episode.duration_seconds
      )
    )
  })
  const visible = showAll ? filtered : filtered.slice(0, collapsedListSize)
  const hasMore = filtered.length > collapsedListSize

  if (!current) {
    return (
      <main className="page-shell">
        <section className="rounded-xl border border-dashed border-line bg-surface p-[clamp(2rem,6vw,4rem)] text-center">
          <h2 className="mt-0">{t("podcastEmptyTitle")}</h2>
          <p className="mb-0 text-sea-ink-soft">
            {t("podcastEmptyDescription")}
          </p>
        </section>
      </main>
    )
  }

  const isLatest = current.id === latest?.id
  const filterLabels: Record<EpisodeFilter, string> = {
    all: t("podcastFilterAll"),
    unheard: t("podcastFilterUnheard"),
    heard: t("podcastFilterHeard"),
  }

  return (
    <main className="mx-auto grid w-full max-w-[1180px] grid-cols-[minmax(0,1fr)_380px] gap-12 px-12 pt-10 pb-16 max-[1000px]:grid-cols-[minmax(0,1fr)] max-[1000px]:px-6 max-[1000px]:pt-8 max-[1000px]:pb-14">
      <section
        className="flex min-w-0 flex-col gap-[22px]"
        aria-label={t("podcastPlayerFor", { title: current.title })}
        data-testid="podcast-player"
      >
        <div className="flex flex-wrap items-center gap-3 text-[13px]">
          {isLatest ? (
            <span className="rounded-full bg-lagoon-deep px-2 py-[3px] text-[11px] leading-none font-semibold text-white">
              {t("podcastToday")}
            </span>
          ) : null}
          <time
            className="text-[15px] font-semibold text-sea-ink tabular-nums"
            dateTime={current.trading_date}
          >
            {dotDate(current.trading_date)}
          </time>
          <span className="text-sea-ink-faint">
            {[
              weekdayLabel(current.trading_date, locale),
              current.audio_created_at
                ? releaseTimeLabel(current.audio_created_at, locale)
                : null,
              duration > 0 ? durationLabel(duration) : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </div>
        <h1 className="m-0 max-w-[640px] text-[44px] leading-[1.25] font-semibold tracking-[-0.02em] [text-wrap:pretty] max-sm:text-[32px]">
          {current.title}
        </h1>
        <p className="m-0 max-w-[600px] text-base leading-[1.8] text-sea-ink-soft [text-wrap:pretty]">
          {current.summary}
        </p>

        <div className="mt-1.5 flex max-w-[620px] flex-col gap-3.5">
          <div
            className="flex h-14 cursor-pointer items-end gap-0.5"
            role="slider"
            tabIndex={0}
            aria-label={t("podcastSeekHint")}
            aria-valuemin={0}
            aria-valuemax={Math.round(duration)}
            aria-valuenow={Math.round(position)}
            aria-valuetext={`${clockTime(position)} / ${clockTime(duration)}`}
            title={t("podcastSeekHint")}
            onClick={seekFromPointer}
            onKeyDown={event => {
              if (event.key === "ArrowLeft") {
                event.preventDefault()
                seek(position - skipSeconds)
              } else if (event.key === "ArrowRight") {
                event.preventDefault()
                seek(position + skipSeconds)
              }
            }}
          >
            {bars.map((height, index) => (
              <span
                key={index}
                className={`flex-1 rounded-[1px] transition-colors duration-150 ${
                  index / bars.length < playedFraction
                    ? "bg-lagoon-deep"
                    : "bg-wave-idle"
                }`}
                style={{ height }}
                aria-hidden="true"
              />
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-[18px]">
            <button
              className="grid h-[52px] w-[52px] shrink-0 place-items-center rounded-full border-0 bg-lagoon-deep p-0 text-white shadow-[0_6px_18px_rgb(31_154_114/30%)] hover:bg-palm active:scale-[.96]"
              type="button"
              aria-label={playing ? t("podcastPause") : t("podcastPlay")}
              aria-pressed={playing}
              onClick={togglePlay}
            >
              {playing ? (
                <span className="flex gap-1" aria-hidden="true">
                  <span className="block h-4 w-1 rounded-[1px] bg-white" />
                  <span className="block h-4 w-1 rounded-[1px] bg-white" />
                </span>
              ) : (
                <span
                  className="ml-[5px] block h-0 w-0 border-y-[10px] border-l-[16px] border-y-transparent border-l-white"
                  aria-hidden="true"
                />
              )}
            </button>
            <div className="min-w-24 text-sm text-sea-ink tabular-nums">
              <span className="font-semibold">{clockTime(position)}</span>
              <span className="text-sea-ink-faint">
                {" "}
                / {clockTime(duration)}
              </span>
            </div>
            <div className="flex-1" />
            <div className="flex flex-wrap items-center justify-end gap-2 max-sm:w-full max-sm:justify-start">
              <button
                className={chipClass}
                type="button"
                aria-label={t("podcastSkipBack", { seconds: skipSeconds })}
                onClick={() => seek(position - skipSeconds)}
              >
                −{skipSeconds}s
              </button>
              <button
                className={chipClass}
                type="button"
                aria-label={t("podcastSkipForward", { seconds: skipSeconds })}
                onClick={() => seek(position + skipSeconds)}
              >
                +{skipSeconds}s
              </button>
              <button
                className={`${chipClass} min-w-[52px] font-semibold text-sea-ink tabular-nums`}
                type="button"
                aria-label={t("podcastSpeed", { speed: formatSpeed(speed) })}
                onClick={cycleSpeed}
              >
                {formatSpeed(speed)}
              </button>
              <a
                className={chipClass}
                ref={downloadRef}
                href={playback?.url ?? "#"}
                download={`${current.trading_date}.mp3`}
                onClick={requestDownload}
              >
                {t("podcastDownload")}
              </a>
              <button
                className={chipClass}
                type="button"
                aria-live="polite"
                onClick={() => void share()}
              >
                {shared ? t("podcastShareCopied") : t("podcastShare")}
              </button>
            </div>
          </div>
          {loading ? (
            <p
              className="m-0 flex items-center gap-2 text-[13px] text-sea-ink-faint"
              role="status"
            >
              <span
                className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-lagoon-deep"
                aria-hidden="true"
              />
              {t("podcastAudioLoading")}
            </p>
          ) : null}
          {unavailable ? (
            <p
              className="m-0 flex flex-wrap items-center gap-3 text-[13px] text-sea-ink-faint"
              role="alert"
            >
              {t("podcastAudioUnavailable")}
              <button
                className={chipClass}
                type="button"
                onClick={() =>
                  void loadAudio(current.id, { play: false, seekTo: null })
                }
              >
                {t("podcastRetryAudio")}
              </button>
            </p>
          ) : null}
          {playback &&
          playback.requested_locale !== playback.resolved_locale ? (
            <p className="m-0 text-[13px] text-sea-ink-faint">
              {t("podcastAudioFallback")}
            </p>
          ) : null}
          {playback ? (
            <audio
              className="hidden"
              ref={audioRef}
              preload="metadata"
              src={playback.url}
              onLoadedMetadata={event => {
                const audio = event.currentTarget
                if (Number.isFinite(audio.duration)) {
                  setMediaDuration(audio.duration)
                }
                audio.playbackRate = speed
                const action = pendingAction.current
                pendingAction.current = null
                const target = clampPosition(
                  action?.seekTo ?? position,
                  audio.duration
                )
                if (target > 0) audio.currentTime = target
                setPosition(target)
                if (action?.play) {
                  void audio.play().catch(() => setPlaying(false))
                }
              }}
              onPlay={() => setPlaying(true)}
              onPause={() => {
                setPlaying(false)
                saveCurrentProgress()
              }}
              onTimeUpdate={event => {
                const audio = event.currentTarget
                setPosition(audio.currentTime)
                if (
                  Math.abs(audio.currentTime - lastSavedAt.current) >=
                  progressSaveIntervalSeconds
                ) {
                  lastSavedAt.current = audio.currentTime
                  saveCurrentProgress()
                }
              }}
              onEnded={() => {
                setPlaying(false)
                saveCurrentProgress()
                if (options.autoplayNext) playNext()
              }}
              onError={() => {
                setPlayback(null)
                setPlaying(false)
                setUnavailable(true)
              }}
            >
              {t("podcastAudioUnsupported")}
            </audio>
          ) : null}
        </div>

        {options.showChapters && segments.length > 0 ? (
          <div className="flex max-w-[620px] flex-col gap-1 border-t border-line pt-[18px]">
            <div className="mb-1.5 flex items-baseline justify-between">
              <span className="text-xs font-semibold tracking-[.1em] text-sea-ink-faint">
                {t("podcastChapters")}
              </span>
              <span className="text-xs text-sea-ink-faint">
                {t("podcastChapterCount", { count: segments.length })}
              </span>
            </div>
            {segments.map(segment => {
              const active = segment.index === activeChapter
              return (
                <button
                  key={segment.index}
                  className={`-mx-3 flex w-[calc(100%+24px)] items-center gap-4 rounded-lg border-0 px-3 py-[9px] text-left text-sm text-sea-ink transition-colors hover:bg-link-hover ${
                    active ? "bg-link-hover" : "bg-transparent"
                  }`}
                  type="button"
                  aria-current={active ? "true" : undefined}
                  onClick={() => seek(segment.start_seconds, { andPlay: true })}
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      active ? "bg-lagoon-deep" : "bg-chip-line"
                    }`}
                    aria-hidden="true"
                  />
                  <span className="w-10 font-semibold text-lagoon-deep tabular-nums">
                    {clockTime(segment.start_seconds)}
                  </span>
                  <span className={`flex-1 ${active ? "font-semibold" : ""}`}>
                    {segment.title}
                  </span>
                  <span className="text-xs text-sea-ink-faint">
                    {durationLabel(segment.lengthSeconds)}
                  </span>
                </button>
              )
            })}
          </div>
        ) : null}

        <div className="flex max-w-[620px] items-center gap-3 border-t border-line pt-[18px] text-[13px] text-sea-ink-muted">
          <span>{t("podcastRelatedReport")}</span>
          <Link
            to="/$locale/reports"
            params={{ locale }}
            className="inline-flex items-center gap-1.5 font-medium text-lagoon-deep no-underline hover:text-palm"
          >
            {t("podcastReportLink", { date: dotDate(current.trading_date) })}
          </Link>
        </div>
      </section>

      <aside
        className="sticky top-24 flex flex-col gap-3.5 self-start border-l border-line pl-8 max-[1000px]:static max-[1000px]:border-t max-[1000px]:border-l-0 max-[1000px]:pt-6 max-[1000px]:pl-0"
        aria-label={t("podcastPastEpisodes")}
      >
        <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="m-0 text-[15px] font-semibold">
            {t("podcastPastEpisodes")}
            <span className="ml-1 text-xs font-normal whitespace-nowrap text-sea-ink-faint">
              {t("podcastEpisodeCount", { count: episodes.length })}
            </span>
          </h2>
          <div className="flex gap-1 text-xs" role="group">
            {episodeFilters.map(candidate => {
              const on = candidate === filter
              return (
                <button
                  key={candidate}
                  className={`rounded-full px-2.5 py-1 text-xs leading-normal transition-colors hover:border-sea-ink ${
                    on
                      ? "border-sea-ink bg-sea-ink text-surface-strong"
                      : "border-chip-line bg-surface-strong text-sea-ink-soft"
                  }`}
                  type="button"
                  aria-pressed={on}
                  onClick={() => {
                    setFilter(candidate)
                    setShowAll(false)
                  }}
                >
                  {filterLabels[candidate]}
                </button>
              )
            })}
          </div>
        </div>
        {visible.length === 0 ? (
          <p className="m-0 py-7 text-center text-[13px] text-sea-ink-faint">
            {t("podcastNoMatch")}
          </p>
        ) : (
          <ul className="m-0 list-none p-0">
            {visible.map(episode => {
              const stored = progress[episode.id]
              const episodeDuration =
                stored?.durationSeconds || episode.duration_seconds || 0
              const status = listeningStatus(
                stored?.positionSeconds ?? 0,
                episodeDuration || null
              )
              const percent =
                episodeDuration > 0
                  ? Math.min(
                      100,
                      ((stored?.positionSeconds ?? 0) / episodeDuration) * 100
                    )
                  : 0
              return (
                <li key={episode.id}>
                  <button
                    className="-mx-2.5 flex w-[calc(100%+20px)] items-start gap-3.5 rounded-[10px] border-0 border-b border-b-line-soft bg-transparent px-2.5 py-3.5 text-left text-sea-ink transition-colors hover:bg-link-hover"
                    type="button"
                    aria-label={t("podcastSelectEpisode", {
                      title: episode.title,
                    })}
                    onClick={() => selectEpisode(episode.id)}
                  >
                    <span
                      className={`mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full border-[1.5px] ${
                        status.kind === "unheard"
                          ? "border-ring-idle"
                          : "border-lagoon-deep"
                      } ${
                        status.kind === "finished"
                          ? "bg-lagoon-tint"
                          : "bg-surface-strong"
                      }`}
                      aria-hidden="true"
                    >
                      <span className="ml-0.5 block h-0 w-0 border-y-[5.5px] border-l-[9px] border-y-transparent border-l-sea-ink" />
                    </span>
                    <span className="flex min-w-0 flex-1 flex-col gap-[5px]">
                      <span className="flex items-center justify-between text-xs text-sea-ink-faint tabular-nums">
                        <span className="font-semibold text-lagoon-deep">
                          {monthDayLabel(episode.trading_date, locale)} ·{" "}
                          {weekdayLabel(episode.trading_date, locale)}
                        </span>
                        <span>
                          {[
                            episodeDuration > 0
                              ? clockTime(episodeDuration)
                              : null,
                            statusLabel(status),
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </span>
                      </span>
                      <span className="text-[15px] leading-[1.45] font-medium [text-wrap:pretty]">
                        {episode.title}
                      </span>
                      {options.showListSummary ? (
                        <span className="line-clamp-2 text-[13px] leading-[1.6] text-sea-ink-muted">
                          {episode.summary}
                        </span>
                      ) : null}
                      <span className="relative mt-0.5 block h-0.5 bg-line-soft">
                        <span
                          className="absolute top-0 left-0 h-full bg-lagoon-deep"
                          style={{ width: `${percent}%` }}
                        />
                      </span>
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
        {hasMore ? (
          <button
            className="mt-1 w-fit border-0 bg-transparent p-0 text-left text-[13px] font-medium text-lagoon-deep hover:text-palm"
            type="button"
            onClick={() => setShowAll(value => !value)}
          >
            {showAll
              ? t("podcastCollapse")
              : t("podcastShowAll", { count: filtered.length })}
          </button>
        ) : null}
      </aside>
    </main>
  )
}

export function PodcastPageSkeleton() {
  const { t } = useTranslation()
  return (
    <main
      className="mx-auto grid w-full max-w-[1180px] grid-cols-[minmax(0,1fr)_380px] gap-12 px-12 pt-10 pb-16 max-[1000px]:grid-cols-[minmax(0,1fr)] max-[1000px]:px-6 max-[1000px]:pt-8 max-[1000px]:pb-14"
      role="status"
      aria-live="polite"
      aria-label={t("loading")}
    >
      <section className="flex min-w-0 animate-pulse flex-col gap-[22px]">
        <div className="h-4 w-48 rounded bg-line-soft" />
        <div className="flex max-w-[640px] flex-col gap-3">
          <div className="h-11 w-full rounded bg-line-soft" />
          <div className="h-11 w-3/4 rounded bg-line-soft" />
        </div>
        <div className="flex max-w-[600px] flex-col gap-2">
          <div className="h-4 w-full rounded bg-line-soft" />
          <div className="h-4 w-11/12 rounded bg-line-soft" />
          <div className="h-4 w-2/3 rounded bg-line-soft" />
        </div>
        <div className="mt-1.5 flex max-w-[620px] flex-col gap-3.5">
          <div className="h-14 w-full rounded bg-line-soft" />
          <div className="flex items-center gap-[18px]">
            <div className="h-[52px] w-[52px] rounded-full bg-line-soft" />
            <div className="h-4 w-24 rounded bg-line-soft" />
          </div>
        </div>
      </section>
      <aside className="flex animate-pulse flex-col gap-3.5 border-l border-line pl-8 max-[1000px]:border-t max-[1000px]:border-l-0 max-[1000px]:pt-6 max-[1000px]:pl-0">
        <div className="h-4 w-32 rounded bg-line-soft" />
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className="flex gap-3.5 py-3.5">
            <div className="h-8 w-8 shrink-0 rounded-full bg-line-soft" />
            <div className="flex flex-1 flex-col gap-2">
              <div className="h-3 w-24 rounded bg-line-soft" />
              <div className="h-4 w-full rounded bg-line-soft" />
              <div className="h-3 w-5/6 rounded bg-line-soft" />
            </div>
          </div>
        ))}
      </aside>
    </main>
  )
}
