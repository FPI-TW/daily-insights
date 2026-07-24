import type {
  Locale,
  PodcastAudioPlayback,
  User,
} from "@daily-insights/api-client"
import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { podcastProgressKey, restoredPosition } from "#/lib/podcast-progress"
import { browserPodcastClient } from "#/lib/podcasts"

export function PodcastPlayer({
  episodeId,
  locale,
  user,
}: {
  episodeId: string
  locale: Locale
  user: User
}) {
  const { t } = useTranslation()
  const audioRef = useRef<HTMLAudioElement>(null)
  const lastSavedAt = useRef(0)
  const [playback, setPlayback] = useState<PodcastAudioPlayback | null>(null)
  const [loading, setLoading] = useState(true)
  const [unavailable, setUnavailable] = useState(false)

  useEffect(() => {
    let active = true
    setLoading(true)
    setUnavailable(false)
    void browserPodcastClient()
      .createAudioUrl(episodeId, locale)
      .then(value => {
        if (active) setPlayback(value)
      })
      .catch(() => {
        if (active) setUnavailable(true)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [episodeId, locale])

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

  if (loading) return <p role="status">{t("podcastAudioLoading")}</p>
  if (unavailable || !playback) {
    return <p role="alert">{t("podcastAudioUnavailable")}</p>
  }

  return (
    <section className="podcast-player" aria-label={t("podcastPlayer")}>
      {playback.requested_locale !== playback.resolved_locale && (
        <p className="podcast-fallback-note">{t("podcastAudioFallback")}</p>
      )}
      <audio
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
            const position = restoredPosition(raw, event.currentTarget.duration)
            if (position !== null) event.currentTarget.currentTime = position
          } catch {
            // Corrupt storage is ignored.
          }
        }}
        onTimeUpdate={event => {
          if (event.currentTarget.currentTime - lastSavedAt.current >= 5) {
            lastSavedAt.current = event.currentTarget.currentTime
            saveProgress()
          }
        }}
        onPause={saveProgress}
        onEnded={saveProgress}
        onError={() => setUnavailable(true)}
      >
        {t("podcastAudioUnsupported")}
      </audio>
    </section>
  )
}
