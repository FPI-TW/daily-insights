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
import { ApiError } from "@daily-insights/api-client"

const { signDirectUploads, completeDirectUpload, invalidate } = vi.hoisted(
  () => ({
    signDirectUploads: vi.fn(),
    completeDirectUpload: vi.fn(),
    invalidate: vi.fn(),
  })
)

vi.mock("#/lib/admin-podcasts", () => ({
  browserPodcastAdminClient: () => ({
    signDirectUploads,
    completeDirectUpload,
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
