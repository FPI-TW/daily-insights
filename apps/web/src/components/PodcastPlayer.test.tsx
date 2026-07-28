import type { PodcastAudioPlayback, User } from "@daily-insights/api-client"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { PodcastPlayer } from "./PodcastPlayer"

const { createAudioUrl, redirectExpiredSession } = vi.hoisted(() => ({
  createAudioUrl: vi.fn(),
  redirectExpiredSession: vi.fn(),
}))

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock("#/lib/podcasts", () => ({
  browserPodcastClient: () => ({ createAudioUrl }),
}))

vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpiredSession,
}))

const playback: PodcastAudioPlayback = {
  episode_id: "00000000-0000-4000-8000-000000000001",
  requested_locale: "zh-hant",
  resolved_locale: "zh-hant",
  asset_id: "00000000-0000-4000-8000-000000000002",
  url: "https://audio.example.test/podcast.mp3",
  expires_in_seconds: 900,
}

const user: User = {
  id: "00000000-0000-4000-8000-000000000003",
  email: "listener@example.test",
  display_name: "Listener",
  system_role: "org_member",
  status: "active",
  must_change_password: false,
  organization_id: "00000000-0000-4000-8000-000000000004",
}

describe("PodcastPlayer", () => {
  beforeEach(() => {
    const values = new Map<string, string>()
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
      key: (index: number) => [...values.keys()][index] ?? null,
      get length() {
        return values.size
      },
    } satisfies Storage)
    createAudioUrl.mockReset()
    redirectExpiredSession.mockReset()
    redirectExpiredSession.mockResolvedValue(false)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it("starts playback after the R2 audio metadata is loaded", async () => {
    createAudioUrl.mockResolvedValue(playback)
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue()

    const { container } = render(
      <PodcastPlayer
        episodeId={playback.episode_id}
        locale="zh-hant"
        user={user}
        title="Daily briefing"
      />
    )

    fireEvent.click(screen.getByRole("button", { name: "podcastListen" }))

    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull())
    const audio = container.querySelector("audio")
    expect(audio).not.toBeNull()
    if (!audio) throw new Error("audio element was not rendered")
    expect(audio).toHaveAttribute("src", playback.url)

    fireEvent.loadedMetadata(audio)

    expect(play).toHaveBeenCalledOnce()
  })

  it("keeps the native controls available when autoplay is blocked", async () => {
    createAudioUrl.mockResolvedValue(playback)
    vi.spyOn(HTMLMediaElement.prototype, "play").mockRejectedValue(
      new DOMException("Playback blocked", "NotAllowedError")
    )

    const { container } = render(
      <PodcastPlayer
        episodeId={playback.episode_id}
        locale="zh-hant"
        user={user}
        title="Daily briefing"
      />
    )

    fireEvent.click(screen.getByRole("button", { name: "podcastListen" }))
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull())
    const audio = container.querySelector("audio")
    expect(audio).not.toBeNull()
    if (!audio) throw new Error("audio element was not rendered")
    fireEvent.loadedMetadata(audio)

    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    )
    expect(audio).toHaveAttribute("controls")
  })
})
