import type {
  PodcastAudioPlayback,
  PodcastEpisodeSummary,
  User,
} from "@daily-insights/api-client"
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { PodcastPage } from "./PodcastPage"
import { podcastProgressKey } from "#/lib/podcast-progress"

const { createAudioUrl, redirectExpiredSession } = vi.hoisted(() => ({
  createAudioUrl: vi.fn(),
  redirectExpiredSession: vi.fn(),
}))

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) =>
      values ? `${key}:${JSON.stringify(values)}` : key,
  }),
}))

vi.mock("@tanstack/react-router", () => ({
  Link: ({
    children,
    ...props
  }: {
    children: React.ReactNode
    [key: string]: unknown
  }) => (
    <a href="#" data-to={String(props.to)}>
      {children}
    </a>
  ),
}))

vi.mock("#/lib/podcasts", () => ({
  browserPodcastClient: () => ({ createAudioUrl }),
}))

vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpiredSession,
}))

const user: User = {
  id: "00000000-0000-4000-8000-000000000003",
  email: "listener@example.test",
  display_name: "Listener",
  system_role: "org_member",
  status: "active",
  must_change_password: false,
  organization_id: "00000000-0000-4000-8000-000000000004",
}

function episode(
  index: number,
  overrides: Partial<PodcastEpisodeSummary> = {}
): PodcastEpisodeSummary {
  const day = String(10 - index).padStart(2, "0")
  return {
    id: `00000000-0000-4000-8000-0000000000${String(index + 10).padStart(2, "0")}`,
    trading_date: `2026-09-${day}`,
    title: `Episode ${index}`,
    summary: `Summary ${index}`,
    locale: "zh-hant",
    cover_asset_id: null,
    duration_seconds: 490,
    audio_created_at: "2026-09-10T07:30:00+08:00",
    chapters: [],
    ...overrides,
  }
}

const latest = episode(0, {
  chapters: [
    { start_seconds: 0, title: "Opening" },
    { start_seconds: 130, title: "Flows" },
    { start_seconds: 410, title: "Watch list" },
  ],
})

function playbackFor(episodeId: string): PodcastAudioPlayback {
  return {
    episode_id: episodeId,
    requested_locale: "zh-hant",
    resolved_locale: "zh-hant",
    asset_id: "00000000-0000-4000-8000-000000000002",
    url: `https://audio.example.test/${episodeId}.mp3`,
    expires_in_seconds: 900,
  }
}

function renderPage(episodes: PodcastEpisodeSummary[] = [latest]) {
  return render(
    <PodcastPage episodes={episodes} locale="zh-hant" user={user} />
  )
}

async function loadedAudio(container: HTMLElement, duration = 490) {
  await waitFor(() => expect(container.querySelector("audio")).not.toBeNull())
  const audio = container.querySelector("audio")
  if (!audio) throw new Error("audio element was not rendered")
  Object.defineProperty(audio, "duration", {
    configurable: true,
    value: duration,
  })
  fireEvent.loadedMetadata(audio)
  return audio
}

describe("PodcastPage", () => {
  let play: ReturnType<typeof vi.spyOn>
  let pause: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    createAudioUrl.mockReset()
    createAudioUrl.mockImplementation((episodeId: string) =>
      Promise.resolve(playbackFor(episodeId))
    )
    redirectExpiredSession.mockReset()
    redirectExpiredSession.mockResolvedValue(false)
    play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {})
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("shows the latest episode with its badge, chapters and report link", () => {
    renderPage([latest, episode(1)])

    expect(screen.getByText("podcastToday")).toBeInTheDocument()
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Episode 0"
    )
    expect(screen.getByText("2026-09-10")).toBeInTheDocument()
    expect(screen.getByText("Opening")).toBeInTheDocument()
    expect(
      screen.getByText('podcastChapterCount:{"count":3}')
    ).toBeInTheDocument()
    expect(screen.getByText("2:10")).toBeInTheDocument()
    expect(
      screen.getByText('podcastReportLink:{"date":"2026-09-10"}')
    ).toHaveAttribute("data-to", "/$locale/reports")
    expect(createAudioUrl).not.toHaveBeenCalled()
  })

  it("hides the chapter list when an episode has none", () => {
    renderPage([episode(0)])
    expect(screen.queryByText("podcastChapters")).not.toBeInTheDocument()
  })

  it("requests the audio URL on play and starts once metadata loads", async () => {
    const { container } = renderPage()

    fireEvent.click(screen.getByRole("button", { name: "podcastPlay" }))
    expect(createAudioUrl).toHaveBeenCalledWith(latest.id, "zh-hant")

    const audio = await loadedAudio(container)
    expect(audio).toHaveAttribute("src", playbackFor(latest.id).url)
    expect(play).toHaveBeenCalledOnce()

    fireEvent.play(audio)
    expect(
      screen.getByRole("button", { name: "podcastPause" })
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "podcastPause" }))
    expect(pause).toHaveBeenCalled()
  })

  it("seeks to a chapter, skips by 15 seconds and cycles the speed", async () => {
    const { container } = renderPage()

    fireEvent.click(screen.getByRole("button", { name: /Flows/ }))
    const audio = await loadedAudio(container)
    expect(audio.currentTime).toBe(130)
    expect(play).toHaveBeenCalledOnce()
    expect(screen.getByRole("button", { name: /Flows/ })).toHaveAttribute(
      "aria-current",
      "true"
    )

    fireEvent.click(
      screen.getByRole("button", { name: 'podcastSkipForward:{"seconds":15}' })
    )
    expect(audio.currentTime).toBe(145)
    fireEvent.click(
      screen.getByRole("button", { name: 'podcastSkipBack:{"seconds":15}' })
    )
    expect(audio.currentTime).toBe(130)
    // The chapter row and the clock both read 2:10 now.
    expect(screen.getAllByText("2:10")).toHaveLength(2)

    const speed = screen.getByRole("button", {
      name: 'podcastSpeed:{"speed":"1.0×"}',
    })
    fireEvent.click(speed)
    expect(audio.playbackRate).toBe(1.25)
    expect(screen.getByText("1.25×")).toBeInTheDocument()
  })

  it("restores stored progress and lists the other episodes with status", async () => {
    const second = episode(1)
    const third = episode(2)
    localStorage.setItem(
      podcastProgressKey(user.id, latest.id, "zh-hant"),
      JSON.stringify({
        positionSeconds: 95,
        durationSeconds: 490,
        updatedAt: "2026-09-10T00:00:00.000Z",
      })
    )
    localStorage.setItem(
      podcastProgressKey(user.id, second.id, "en"),
      JSON.stringify({
        positionSeconds: 245,
        durationSeconds: 490,
        updatedAt: "2026-09-10T00:00:00.000Z",
      })
    )
    localStorage.setItem(
      podcastProgressKey(user.id, third.id, "zh-hant"),
      JSON.stringify({
        positionSeconds: 490,
        durationSeconds: 490,
        updatedAt: "2026-09-10T00:00:00.000Z",
      })
    )

    renderPage([latest, second, third, episode(3)])

    await waitFor(() => expect(screen.getByText("1:35")).toBeInTheDocument())
    expect(
      screen.getByText(/podcastStatusPartial:\{"percent":50\}/)
    ).toBeInTheDocument()
    expect(screen.getByText(/podcastStatusFinished/)).toBeInTheDocument()
    expect(screen.getByText(/podcastStatusUnheard/)).toBeInTheDocument()

    fireEvent.click(
      screen.getByRole("button", { name: "podcastFilterUnheard" })
    )
    expect(screen.getByText("Episode 3")).toBeInTheDocument()
    expect(screen.queryByText("Episode 1")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "podcastFilterHeard" }))
    expect(screen.getByText("Episode 1")).toBeInTheDocument()
    expect(screen.getByText("Episode 2")).toBeInTheDocument()
    expect(screen.queryByText("Episode 3")).not.toBeInTheDocument()
  })

  it("collapses the list to four episodes until expanded", () => {
    renderPage(Array.from({ length: 7 }, (_, index) => episode(index)))

    expect(screen.getAllByRole("listitem")).toHaveLength(4)
    fireEvent.click(
      screen.getByRole("button", { name: 'podcastShowAll:{"count":6}' })
    )
    expect(screen.getAllByRole("listitem")).toHaveLength(6)
    fireEvent.click(screen.getByRole("button", { name: "podcastCollapse" }))
    expect(screen.getAllByRole("listitem")).toHaveLength(4)
  })

  it("switches to a past episode, resumes it and moves the latest to the list", async () => {
    const second = episode(1)
    localStorage.setItem(
      podcastProgressKey(user.id, second.id, "zh-hant"),
      JSON.stringify({
        positionSeconds: 60,
        durationSeconds: 490,
        updatedAt: "2026-09-10T00:00:00.000Z",
      })
    )
    const { container } = renderPage([latest, second])
    await waitFor(() =>
      expect(
        screen.getByText(/podcastStatusPartial:\{"percent":12\}/)
      ).toBeInTheDocument()
    )

    fireEvent.click(
      screen.getByRole("button", {
        name: 'podcastSelectEpisode:{"title":"Episode 1"}',
      })
    )
    expect(createAudioUrl).toHaveBeenCalledWith(second.id, "zh-hant")
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Episode 1"
    )
    expect(screen.queryByText("podcastToday")).not.toBeInTheDocument()
    expect(
      screen.getByRole("button", {
        name: 'podcastSelectEpisode:{"title":"Episode 0"}',
      })
    ).toBeInTheDocument()

    const audio = await loadedAudio(container)
    expect(audio.currentTime).toBe(60)
    expect(play).toHaveBeenCalledOnce()
  })

  it("copies a share link for the current episode", async () => {
    vi.useFakeTimers()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } })
    renderPage()

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "podcastShare" }))
    })
    expect(writeText).toHaveBeenCalledWith(
      `http://localhost/zh-hant/podcasts?episode=${latest.id}`
    )
    expect(screen.getByText("podcastShareCopied")).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(1600)
    })
    expect(screen.getByText("podcastShare")).toBeInTheDocument()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it("shows a retryable message when the audio URL fails", async () => {
    createAudioUrl.mockRejectedValueOnce(new Error("boom"))
    const { container } = renderPage()

    fireEvent.click(screen.getByRole("button", { name: "podcastPlay" }))
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "podcastAudioUnavailable"
      )
    )
    fireEvent.click(screen.getByRole("button", { name: "podcastRetryAudio" }))
    await loadedAudio(container)
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(play).not.toHaveBeenCalled()
  })

  it("renders the empty state without episodes", () => {
    renderPage([])
    expect(screen.getByText("podcastEmptyTitle")).toBeInTheDocument()
  })
})
