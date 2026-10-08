import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import { createI18n } from "#/lib/i18n"
import { AudioManagementPage } from "./AudioManagementPage"
import { ApiError, type PodcastEpisodeAdmin } from "@daily-insights/api-client"

const { signDirectUploads, completeDirectUpload, removeEpisode, invalidate } =
  vi.hoisted(() => ({
    signDirectUploads: vi.fn(),
    completeDirectUpload: vi.fn(),
    removeEpisode: vi.fn(),
    invalidate: vi.fn(),
  }))

vi.mock("#/lib/admin-podcasts", () => ({
  browserPodcastAdminClient: () => ({
    signDirectUploads,
    completeDirectUpload,
    remove: removeEpisode,
  }),
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => vi.fn().mockResolvedValue(false),
}))
vi.mock("@tanstack/react-router", () => ({ useRouter: () => ({ invalidate }) }))

class MockUploadRequest {
  static last: MockUploadRequest | null = null
  static statusQueue: number[] = []
  upload: {
    onprogress:
      | ((event: {
          lengthComputable: boolean
          loaded: number
          total: number
        }) => void)
      | null
  } = { onprogress: null }
  status = 412
  withCredentials = true
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  headers: Record<string, string> = {}
  constructor() {
    MockUploadRequest.last = this
  }
  open = vi.fn()
  setRequestHeader = (name: string, value: string) => {
    this.headers[name] = value
  }
  send = vi.fn(() => {
    this.status = MockUploadRequest.statusQueue.shift() ?? this.status
    this.upload.onprogress?.({ lengthComputable: true, loaded: 10, total: 10 })
    this.onload?.()
  })
  abort = vi.fn(() => this.onabort?.())
}

afterEach(() => {
  cleanup()
  MockUploadRequest.statusQueue = []
  signDirectUploads.mockReset()
  completeDirectUpload.mockReset()
  invalidate.mockReset()
  removeEpisode.mockReset()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

function selectFile(input: HTMLElement, file: File) {
  fireEvent.change(input, {
    target: {
      files: Object.assign([file], {
        item(index: number) {
          return index === 0 ? file : null
        },
      }),
    },
  })
}

const target = {
  asset_id: "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22",
  locale: "en",
  upload_url: "https://storage.example/signed",
  upload_token: "signed-receipt",
  required_headers: {
    "Content-Type": "audio/mpeg",
    "If-None-Match": "*",
    "x-amz-meta-sha256": "0".repeat(64),
  },
  expires_at: "2999-01-01T00:00:00Z",
}
const completed = { status: "completed", sha256: "0".repeat(64) }

function setup() {
  vi.stubGlobal("XMLHttpRequest", MockUploadRequest)
  vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
    new Uint8Array(32).buffer
  )
  signDirectUploads.mockResolvedValue({ files: [target] })
  completeDirectUpload.mockResolvedValue(completed)
  render(
    <I18nextProvider i18n={createI18n("en")}>
      <AudioManagementPage episodes={[]} canPublish locale="en" />
    </I18nextProvider>
  )
  fireEvent.change(screen.getByLabelText("Trading date"), {
    target: { value: "2026-10-01" },
  })
  selectFile(
    screen.getAllByLabelText(/Click to choose or drop an audio file here/)[2]!,
    new File(["podcast"], "episode.mp3", { type: "audio/mpeg" })
  )
}

async function submit() {
  fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))
  await waitFor(() => expect(signDirectUploads).toHaveBeenCalled())
}

describe("AudioManagementPage synchronous direct uploads", () => {
  it.each([
    ["episode", "audio/mpeg"],
    ["mp3", "audio/mpeg"],
    ["episode.", "audio/mpeg"],
    ["episode.mp3.exe", "audio/mpeg"],
    ["episode.wav", "audio/wav"],
    ["episode.mp3", "video/mp4"],
    ["episode.mp4", "audio/mpeg"],
  ])(
    "rejects selection and drop of %s (%s) before hashing or networking",
    (name, type) => {
      setup()
      const input = document.getElementById("podcast-file-en")!
      const invalid = new File(["bad"], name, { type })
      selectFile(input, invalid)
      expect(screen.getByRole("alert")).toHaveTextContent(".mp3 or .mp4")
      expect(screen.getByText("episode.mp3")).toBeVisible()
      fireEvent.drop(input.parentElement!, {
        dataTransfer: { files: { item: () => invalid } },
      })
      expect(screen.getByRole("alert")).toHaveTextContent(".mp3 or .mp4")
      expect(screen.getByText("episode.mp3")).toBeVisible()
      expect(crypto.subtle.digest).not.toHaveBeenCalled()
      expect(signDirectUploads).not.toHaveBeenCalled()
      expect(completeDirectUpload).not.toHaveBeenCalled()
      expect(input).toHaveValue("")
    }
  )

  it.each([
    ["episode.MP3", "audio/mp3", "audio/mpeg", "select"],
    ["episode.MP4", "video/mp4", "audio/mp4", "drop"],
    ["episode.Mp3", "", "audio/mpeg", "drop"],
    ["episode.Mp4", "", "audio/mp4", "select"],
  ])(
    "accepts %s via %s and clears its slot error",
    async (name, type, mime, method) => {
      setup()
      const input = document.getElementById("podcast-file-en")!
      selectFile(input, new File(["bad"], "bad.exe"))
      const valid = new File(["podcast"], name, { type })
      if (method === "drop") {
        fireEvent.drop(input.parentElement!, {
          dataTransfer: { files: { item: () => valid } },
        })
      } else selectFile(input, valid)
      expect(screen.queryByRole("alert")).not.toBeInTheDocument()
      await submit()
      await screen.findByText("Complete")
      expect(signDirectUploads.mock.calls[0]?.[0].files[0]).toMatchObject({
        filename: name,
        mime_type: mime,
      })
    }
  )

  it("clears a selection error when removing the retained file", () => {
    setup()
    selectFile(
      document.getElementById("podcast-file-en")!,
      new File(["bad"], "bad.exe")
    )
    fireEvent.click(screen.getByRole("button", { name: "Remove" }))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.queryByText("episode.mp3")).not.toBeInTheDocument()
  })

  it("preserves replacement confirmation after invalid selection", async () => {
    setup()
    signDirectUploads.mockRejectedValueOnce(
      new ApiError(409, null, "Replacement required", {
        code: "replacement_confirmation_required",
        current_versions: { en: 2 },
      })
    )
    await submit()
    await screen.findByRole("button", { name: "Confirm overwrite" })
    selectFile(
      document.getElementById("podcast-file-en")!,
      new File(["bad"], "bad.exe")
    )
    expect(
      screen.getByRole("button", { name: "Confirm overwrite" })
    ).toBeVisible()
    fireEvent.click(screen.getByRole("button", { name: "Confirm overwrite" }))
    await screen.findByText("Complete")
    expect(signDirectUploads.mock.calls[1]?.[0].files[0]).toMatchObject({
      filename: "episode.mp3",
      expected_current_version: 2,
    })
  })

  it("preserves a failed batch and retry receipt after invalid drop", async () => {
    setup()
    completeDirectUpload.mockRejectedValueOnce(new Error("network"))
    await submit()
    await screen.findByText("Failed")
    const input = document.getElementById("podcast-file-en")!
    fireEvent.drop(input.parentElement!, {
      dataTransfer: { files: { item: () => new File(["bad"], "bad.exe") } },
    })
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible()
    fireEvent.click(screen.getByRole("button", { name: "Retry" }))
    await screen.findByText("Complete")
    expect(signDirectUploads).toHaveBeenCalledTimes(1)
    expect(completeDirectUpload).toHaveBeenCalledTimes(2)
  })

  it("validates the retained file again at submit before hashing or signing", async () => {
    setup()
    const changed = new File(["podcast"], "valid.mp3", { type: "audio/mpeg" })
    selectFile(document.getElementById("podcast-file-en")!, changed)
    Object.defineProperty(changed, "name", { value: "spoof.mp3.exe" })
    fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(".mp3 or .mp4")
    expect(crypto.subtle.digest).not.toHaveBeenCalled()
    expect(signDirectUploads).not.toHaveBeenCalled()
  })

  it("uploads with signed headers and finishes without polling", async () => {
    setup()
    await submit()
    await screen.findByText("Complete")
    expect(completeDirectUpload).toHaveBeenCalledWith("signed-receipt", "csrf")
    expect(MockUploadRequest.last?.headers).toEqual(target.required_headers)
    expect(MockUploadRequest.last?.withCredentials).toBe(false)
    expect(screen.getByRole("button", { name: "Upload audio" })).toBeEnabled()
    expect(invalidate).toHaveBeenCalled()
  })

  it.each(["normal completion", "completion retry"])(
    "submits only the newly selected locale after %s",
    async path => {
      setup()
      signDirectUploads
        .mockResolvedValueOnce({ files: [target] })
        .mockResolvedValue({
          files: [
            {
              ...target,
              locale: "zh-hant",
              upload_token: "traditional-receipt",
            },
          ],
        })
      if (path === "completion retry") {
        completeDirectUpload.mockRejectedValueOnce(new Error("response lost"))
      }
      await submit()
      if (path === "completion retry") {
        await screen.findByText("Failed")
        expect(screen.getByText("episode.mp3")).toBeVisible()
        fireEvent.click(screen.getByRole("button", { name: "Retry" }))
      }
      await screen.findByText("Complete")
      selectFile(
        document.getElementById("podcast-file-zh-hant")!,
        new File(["traditional"], "traditional.mp3", { type: "audio/mpeg" })
      )
      await submit()
      await waitFor(() => expect(signDirectUploads).toHaveBeenCalledTimes(2))
      expect(signDirectUploads.mock.calls[1]?.[0].files).toEqual([
        expect.objectContaining({
          locale: "zh-hant",
          filename: "traditional.mp3",
        }),
      ])
      expect(screen.queryByText("episode.mp3")).not.toBeInTheDocument()
    }
  )

  it("keeps verification pending until the server finishes", async () => {
    setup()
    let finish!: (value: typeof completed) => void
    completeDirectUpload.mockImplementation(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )
    await submit()
    await screen.findByText("Verifying and saving audio…")
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled()
    finish(completed)
    await screen.findByText("Complete")
  })

  it("allows starting a new upload after a failure", async () => {
    setup()
    completeDirectUpload.mockRejectedValue(
      new ApiError(409, null, "Missing object", { code: "object_not_uploaded" })
    )
    await submit()
    await screen.findByText("Failed")
    expect(screen.getByLabelText("Trading date")).toBeEnabled()
    expect(screen.getByRole("button", { name: "Upload audio" })).toBeEnabled()
    await submit()
    await waitFor(() => expect(signDirectUploads).toHaveBeenCalledTimes(2))
  })

  it("retries completion after a lost response without another PUT", async () => {
    setup()
    completeDirectUpload
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue(completed)
    await submit()
    await screen.findByText("Failed")
    const previousRequest = MockUploadRequest.last
    fireEvent.click(screen.getByRole("button", { name: "Retry" }))
    await screen.findByText("Complete")
    expect(MockUploadRequest.last).toBe(previousRequest)
    expect(signDirectUploads).toHaveBeenCalledTimes(1)
    expect(completeDirectUpload).toHaveBeenCalledTimes(2)
  })

  it("starts a fresh signed upload after an expired receipt", async () => {
    setup()
    signDirectUploads.mockResolvedValueOnce({
      files: [{ ...target, expires_at: "2000-01-01T00:00:00Z" }],
    })
    completeDirectUpload
      .mockRejectedValueOnce(
        new ApiError(410, null, "Expired", { code: "upload_token_expired" })
      )
      .mockRejectedValueOnce(
        new ApiError(410, null, "Expired", { code: "upload_token_expired" })
      )
      .mockResolvedValue(completed)
    await submit()
    await screen.findByText("Failed")
    fireEvent.click(screen.getByRole("button", { name: "Retry" }))
    await screen.findByText("Complete")
    expect(signDirectUploads).toHaveBeenCalledTimes(2)
  })

  it("requires confirmation before replacing an existing locale", async () => {
    setup()
    signDirectUploads
      .mockRejectedValueOnce(
        new ApiError(409, null, "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 2 },
        })
      )
      .mockResolvedValue({ files: [target] })
    await submit()
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm overwrite" })
    )
    await screen.findByText("Complete")
    expect(signDirectUploads.mock.calls[1]?.[0].files[0]).toMatchObject({
      confirm_replacement: true,
      expected_current_version: 2,
    })
  })
  it("discards an old receipt when the selected file changes after a failure", async () => {
    setup()
    completeDirectUpload.mockRejectedValueOnce(new Error("network"))
    await submit()
    await screen.findByText("Failed")
    selectFile(
      document.getElementById("podcast-file-en")!,
      new File(["replacement"], "different.mp3", { type: "audio/mpeg" })
    )
    expect(
      screen.queryByRole("button", { name: "Retry" })
    ).not.toBeInTheDocument()
    await submit()
    await screen.findByText("Complete")
    expect(signDirectUploads.mock.calls[1]?.[0].files[0].filename).toBe(
      "different.mp3"
    )
  })

  it("keeps a successful locale active when another locale fails", async () => {
    setup()
    selectFile(
      screen.getAllByLabelText(
        /Click to choose or drop an audio file here/
      )[0]!,
      new File(["traditional"], "traditional.mp3", { type: "audio/mpeg" })
    )
    signDirectUploads.mockResolvedValue({
      files: [
        target,
        { ...target, locale: "zh-hant", upload_token: "failed-receipt" },
      ],
    })
    completeDirectUpload.mockImplementation(token =>
      token === "failed-receipt"
        ? Promise.reject(new Error("storage temporarily unavailable"))
        : Promise.resolve(completed)
    )
    await submit()
    await screen.findByText("Complete")
    await screen.findByText("Failed")
    expect(screen.queryByText("episode.mp3")).not.toBeInTheDocument()
    expect(screen.getByText("traditional.mp3")).toBeVisible()
    expect(screen.getByRole("button", { name: "Upload audio" })).toBeEnabled()
    expect(invalidate).toHaveBeenCalled()
    completeDirectUpload.mockResolvedValue(completed)
    const previousRequest = MockUploadRequest.last
    fireEvent.click(screen.getByRole("button", { name: "Retry" }))
    await waitFor(() =>
      expect(screen.queryByText("traditional.mp3")).not.toBeInTheDocument()
    )
    expect(completeDirectUpload).toHaveBeenLastCalledWith(
      "failed-receipt",
      "csrf"
    )
    expect(MockUploadRequest.last).toBe(previousRequest)
    expect(signDirectUploads).toHaveBeenCalledTimes(1)
  })
})

const removableEpisode: PodcastEpisodeAdmin = {
  id: "10000000-0000-4000-8000-000000000001",
  trading_date: "2026-10-01",
  status: "draft",
  version: 1,
  metadata: [{ locale: "en", title: "Removal test", summary: "Summary" }],
  metadata_source: "derived",
  audio_variants: [],
  cover_asset_id: null,
  published_at: null,
}

function removalPage(episode: PodcastEpisodeAdmin = removableEpisode) {
  return (
    <I18nextProvider i18n={createI18n("en")}>
      <AudioManagementPage episodes={[episode]} canPublish locale="en" />
    </I18nextProvider>
  )
}

describe("AudioManagementPage whole episode removal", () => {
  it("disables removal on published episodes and explains unpublishing first", () => {
    render(removalPage({ ...removableEpisode, status: "published" }))
    expect(
      screen.getByRole("button", { name: "Permanently remove" })
    ).toBeDisabled()
    expect(
      screen.getByText("Unpublish this episode before removing it.")
    ).toBeVisible()
  })

  it("confirms date, all languages, history, and irreversibility; cancel sends no request", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    render(removalPage())
    fireEvent.click(screen.getByRole("button", { name: "Permanently remove" }))
    expect(confirm).toHaveBeenCalledWith(
      expect.stringMatching(
        /2026-10-01.*All languages, historical audio.*cannot be undone/
      )
    )
    expect(removeEpisode).not.toHaveBeenCalled()
    expect(invalidate).not.toHaveBeenCalled()
  })

  it("keeps the card and disables actions while removal is pending", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true)
    let finish!: () => void
    removeEpisode.mockReturnValue(
      new Promise<void>(resolve => {
        finish = resolve
      })
    )
    render(removalPage())
    fireEvent.click(screen.getByRole("button", { name: "Permanently remove" }))
    await screen.findByText("Removing the entire Podcast…")
    expect(screen.getByText("Removal test")).toBeVisible()
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled()
    expect(
      screen.getByRole("button", { name: "Permanently remove" })
    ).toBeDisabled()
    expect(invalidate).not.toHaveBeenCalled()
    finish()
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ sync: true }))
    expect(removeEpisode).toHaveBeenCalledWith(
      removableEpisode.id,
      { expected_version: 1 },
      "csrf"
    )
  })

  it("refreshes failed removal progress and retries using the refreshed version", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true)
    removeEpisode
      .mockRejectedValueOnce(
        new ApiError(503, "req", "failed", {
          code: "episode_removal_incomplete",
        })
      )
      .mockResolvedValueOnce(undefined)
    const view = render(removalPage())
    fireEvent.click(screen.getByRole("button", { name: "Permanently remove" }))
    await screen.findByText(
      "Removal is incomplete and the episode is frozen. Retry to clear the remaining files."
    )
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ sync: true }))
    view.rerender(
      removalPage({
        ...removableEpisode,
        version: 2,
        deletion: {
          status: "pending",
          total_objects: 3,
          cleared_objects: 1,
          retained_objects: 0,
        },
      })
    )
    await screen.findByText(
      "Removal incomplete: 1 / 3 files processed. Retry manually to continue."
    )
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "Retry removal" }))
    await waitFor(() =>
      expect(removeEpisode).toHaveBeenLastCalledWith(
        removableEpisode.id,
        { expected_version: 2 },
        "csrf"
      )
    )
  })
})
