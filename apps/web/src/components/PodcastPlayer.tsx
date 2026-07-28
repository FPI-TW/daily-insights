import type {
  Locale,
  PodcastAudioPlayback,
  User,
} from "@daily-insights/api-client"
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react"
import { useTranslation } from "react-i18next"
import { podcastProgressKey, restoredPosition } from "#/lib/podcast-progress"
import { browserPodcastClient } from "#/lib/podcasts"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

export function PodcastPlayer({
  episodeId,
  locale,
  user,
  title,
}: {
  episodeId: string
  locale: Locale
  user: User
  title: string
}) {
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "customer")
  const audioRef = useRef<HTMLAudioElement>(null)
  const lastSavedAt = useRef(0)
  const requestGeneration = useRef(0)
  const [playback, setPlayback] = useState<PodcastAudioPlayback | null>(null)
  const [requested, setRequested] = useState(false)
  const [loading, setLoading] = useState(false)
  const [unavailable, setUnavailable] = useState(false)

  const loadAudio = useCallback(async () => {
    const generation = ++requestGeneration.current
    setRequested(true)
    setLoading(true)
    setUnavailable(false)
    setPlayback(null)
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
      setUnavailable(true)
    } finally {
      if (generation === requestGeneration.current) setLoading(false)
    }
  }, [episodeId, locale, redirectExpiredSession])

  useLayoutEffect(() => {
    return () => {
      requestGeneration.current += 1
    }
  }, [])

  function saveProgress() {
    const audio = audioRef.current
    if (!audio || !playback || !Number.isFinite(audio.duration)) return
    try {
      localStorage.setItem(
        podcastProgressKey(user.id, episodeId, playback.resolved_locale),
        JSON.stringify({
          positionSeconds: audio.currentTime,
          durationSeconds: audio.duration,
          updatedAt: new Date().toISOString(),
        })
      )
    } catch {
      // Storage must never block playback.
    }
  }

  useEffect(() => {
    const saveBeforeUnload = () => saveProgress()
    window.addEventListener("beforeunload", saveBeforeUnload)
    return () => {
      saveProgress()
      window.removeEventListener("beforeunload", saveBeforeUnload)
    }
  })

  return (
    <section
      className="mt-[0.85rem] grid gap-3 max-[42rem]:col-span-full"
      aria-label={t("podcastPlayerFor", { title })}
      data-testid="podcast-player"
    >
      {!requested ? (
        <button
          className="w-fit border-[color-mix(in_oklab,var(--lagoon-deep)_30%,var(--line))] px-3 py-2 text-[0.8rem] font-extrabold text-lagoon-deep"
          type="button"
          onClick={() => void loadAudio()}
        >
          {t("podcastListen")}
        </button>
      ) : null}
      {loading ? <p role="status">{t("podcastAudioLoading")}</p> : null}
      {unavailable ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="m-0" role="alert">
            {t("podcastAudioUnavailable")}
          </p>
          <button
            className="w-fit"
            type="button"
            onClick={() => void loadAudio()}
          >
            {t("podcastRetryAudio")}
          </button>
        </div>
      ) : null}
      {playback && playback.requested_locale !== playback.resolved_locale ? (
        <p className="m-0 text-[0.85rem] text-sea-ink-soft">
          {t("podcastAudioFallback")}
        </p>
      ) : null}
      {playback ? (
        <audio
          className="w-full"
          ref={audioRef}
          controls
          controlsList="nodownload"
          preload="metadata"
          src={playback.url}
          onLoadedMetadata={event => {
            try {
              const raw = localStorage.getItem(
                podcastProgressKey(user.id, episodeId, playback.resolved_locale)
              )
              const position = restoredPosition(
                raw,
                event.currentTarget.duration
              )
              if (position !== null) event.currentTarget.currentTime = position
            } catch {
              // Corrupt storage is ignored.
            }
            void event.currentTarget.play().catch(() => {
              // Browser autoplay policy may require the native play control.
            })
          }}
          onTimeUpdate={event => {
            if (event.currentTarget.currentTime - lastSavedAt.current >= 5) {
              lastSavedAt.current = event.currentTarget.currentTime
              saveProgress()
            }
          }}
          onPause={saveProgress}
          onEnded={saveProgress}
          onError={() => {
            setPlayback(null)
            setUnavailable(true)
          }}
        >
          {t("podcastAudioUnsupported")}
        </audio>
      ) : null}
    </section>
  )
}
