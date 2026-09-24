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

const {
  initializeUploadBatch,
  finalizeUploadBatchFile,
  uploadBatchStatus,
  invalidate,
} = vi.hoisted(() => ({
  initializeUploadBatch: vi.fn(),
  finalizeUploadBatchFile: vi.fn(),
  uploadBatchStatus: vi.fn(),
  invalidate: vi.fn(),
}))

vi.mock("#/lib/admin-podcasts", () => ({
  browserPodcastAdminClient: () => ({
    initializeUploadBatch,
    finalizeUploadBatchFile,
    uploadBatchStatus,
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
  initializeUploadBatch.mockReset()
  finalizeUploadBatchFile.mockReset()
  uploadBatchStatus.mockReset()
  invalidate.mockReset()
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

describe("AudioManagementPage direct uploads", () => {
  it("recovers a processing upload to completed state", async () => {
    const originalXhr = globalThis.XMLHttpRequest
    globalThis.XMLHttpRequest =
      MockUploadRequest as unknown as typeof XMLHttpRequest
    const digest = vi
      .spyOn(globalThis.crypto.subtle, "digest")
      .mockResolvedValue(new Uint8Array(32).buffer)
    initializeUploadBatch
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 2 },
        })
      )
      .mockResolvedValue({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: null,
        status: "pending",
        expires_at: "2026-07-25T10:00:00Z",
        files: [
          {
            session_id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11",
            asset_id: "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22",
            locale: "en",
            object_key: "audio/en.mp3",
            upload_url: "https://storage.example/signed",
            required_headers: {
              "Content-Type": "audio/mpeg",
              "If-None-Match": "*",
              "x-amz-meta-sha256": "0".repeat(64),
            },
            expires_at: "2999-01-01T00:00:00Z",
            status: "pending_upload",
            request_id: "request-id",
          },
        ],
      })
    finalizeUploadBatchFile.mockResolvedValue({ status: "queued" })
    uploadBatchStatus
      .mockResolvedValueOnce({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        status: "pending",
        applied_count: 0,
        files: [
          {
            session_id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11",
            asset_id: "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22",
            locale: "en",
            status: "processing",
            error_code: "transient_worker_error",
            sha256: null,
            duration_seconds: null,
          },
        ],
      })
      .mockResolvedValue({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        status: "completed",
        applied_count: 1,
        files: [
          {
            session_id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11",
            asset_id: "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22",
            locale: "en",
            status: "completed",
            error_code: null,
            sha256: "0".repeat(64),
            duration_seconds: 10,
          },
        ],
      })

    try {
      render(
        <I18nextProvider i18n={createI18n("en")}>
          <AudioManagementPage episodes={[]} canPublish locale="en" />
        </I18nextProvider>
      )
      fireEvent.change(screen.getByLabelText("Trading date"), {
        target: { value: "2026-07-25" },
      })
      const audioFile = new File(["0123456789"], "episode.mp3", {
        type: "audio/mpeg",
      })
      fireEvent.change(
        screen.getAllByLabelText(
          "Click to choose or drop an audio file here"
        )[2]!,
        {
          target: {
            files: Object.assign([audioFile], {
              item(index: number) {
                return index === 0 ? audioFile : null
              },
            }),
          },
        }
      )
      fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))
      const confirmReplacement = await screen.findByRole("button", {
        name: "Confirm overwrite",
      })
      fireEvent.click(confirmReplacement)

      await screen.findByText(
        "Complete",
        {
          selector: "span",
        },
        { timeout: 3_000 }
      )
      const firstAttempt = initializeUploadBatch.mock.calls[0]?.[0]
      const confirmedAttempt = initializeUploadBatch.mock.calls[1]?.[0]
      expect(firstAttempt?.idempotency_key).not.toBe(
        confirmedAttempt?.idempotency_key
      )
      expect(initializeUploadBatch).toHaveBeenCalledWith(
        expect.objectContaining({
          trading_date: "2026-07-25",
          files: [
            {
              locale: "en",
              filename: "episode.mp3",
              size_bytes: 10,
              mime_type: "audio/mpeg",
              sha256: "0".repeat(64),
            },
          ],
        }),
        "csrf"
      )
      expect(confirmedAttempt.files[0]).toMatchObject({
        confirm_replacement: true,
        expected_current_version: 2,
      })
      expect(finalizeUploadBatchFile).toHaveBeenCalledWith(
        "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        "en",
        "csrf"
      )
      expect(
        screen.getByText("Complete", { selector: "span" })
      ).toBeInTheDocument()
      expect(
        screen.getByText(`Verified SHA-256: ${"0".repeat(64)}`)
      ).toBeInTheDocument()
      expect(
        screen.queryByText("transient_worker_error")
      ).not.toBeInTheDocument()
      expect(MockUploadRequest.last?.withCredentials).toBe(false)
      expect(digest).toHaveBeenCalledWith("SHA-256", expect.any(ArrayBuffer))
      expect(MockUploadRequest.last?.headers).toEqual({
        "Content-Type": "audio/mpeg",
        "If-None-Match": "*",
        "x-amz-meta-sha256": "0".repeat(64),
      })
    } finally {
      globalThis.XMLHttpRequest = originalXhr
    }
  })

  it("confirms replacement only for locales returned by the conflict", async () => {
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    initializeUploadBatch
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 4 },
        })
      )
      .mockResolvedValueOnce({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: 4,
        status: "pending",
        expires_at: "2026-07-25T10:00:00Z",
        files: [],
      })
    uploadBatchStatus.mockResolvedValue({
      batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
      status: "pending",
      applied_count: 0,
      files: [],
    })

    render(
      <I18nextProvider i18n={createI18n("en")}>
        <AudioManagementPage episodes={[]} canPublish locale="en" />
      </I18nextProvider>
    )
    fireEvent.change(screen.getByLabelText("Trading date"), {
      target: { value: "2026-07-25" },
    })
    const slots = screen.getAllByLabelText(
      "Click to choose or drop an audio file here"
    )
    for (const [index, name] of ["zh-hant.mp3", "episode.mp3"].entries()) {
      const audioFile = new File([`audio ${index}`], name, {
        type: "audio/mpeg",
      })
      fireEvent.change(slots[index === 0 ? 0 : 2]!, {
        target: {
          files: Object.assign([audioFile], {
            item(fileIndex: number) {
              return fileIndex === 0 ? audioFile : null
            },
          }),
        },
      })
    }
    fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm overwrite" })
    )

    await waitFor(() => expect(initializeUploadBatch).toHaveBeenCalledTimes(2))
    const confirmedFiles = initializeUploadBatch.mock.calls[1]?.[0]
      .files as Array<{
      locale: string
      confirm_replacement?: boolean
      expected_current_version?: number
    }>
    expect(confirmedFiles).toHaveLength(2)
    expect(
      confirmedFiles?.find(file => file.locale === "zh-hant")
    ).not.toHaveProperty("confirm_replacement")
    expect(confirmedFiles?.find(file => file.locale === "en")).toMatchObject({
      confirm_replacement: true,
      expected_current_version: 4,
    })
  })

  it("clears a replacement warning when the trading date changes", async () => {
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    initializeUploadBatch
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 4 },
        })
      )
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 5 },
        })
      )
      .mockResolvedValueOnce({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        trading_date: "2026-07-26",
        reason: "initial_upload",
        base_episode_version: null,
        status: "pending",
        expires_at: "2026-07-25T10:00:00Z",
        files: [],
      })
    uploadBatchStatus.mockResolvedValue({
      batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
      status: "pending",
      applied_count: 0,
      files: [],
    })

    render(
      <I18nextProvider i18n={createI18n("en")}>
        <AudioManagementPage episodes={[]} canPublish locale="en" />
      </I18nextProvider>
    )
    const date = screen.getByLabelText("Trading date")
    fireEvent.change(date, { target: { value: "2026-07-25" } })
    selectFile(
      screen.getAllByLabelText(
        "Click to choose or drop an audio file here"
      )[2]!,
      new File(["audio"], "episode.mp3", { type: "audio/mpeg" })
    )
    const submit = screen.getByRole("button", { name: "Upload audio" })
    fireEvent.click(submit)
    expect(
      await screen.findByRole("button", { name: "Confirm overwrite" })
    ).toBeInTheDocument()

    fireEvent.change(date, { target: { value: "2026-07-26" } })
    expect(
      screen.queryByRole("button", { name: "Confirm overwrite" })
    ).not.toBeInTheDocument()
    fireEvent.click(submit)
    expect(
      await screen.findByRole("button", { name: "Confirm overwrite" })
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Confirm overwrite" }))

    await waitFor(() => expect(initializeUploadBatch).toHaveBeenCalledTimes(3))
    expect(initializeUploadBatch.mock.calls[2]?.[0]).toMatchObject({
      trading_date: "2026-07-26",
      files: [{ confirm_replacement: true, expected_current_version: 5 }],
    })
  })

  it("clears a replacement warning when file and locale selection changes", async () => {
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    initializeUploadBatch
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 4 },
        })
      )
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 5 },
        })
      )
      .mockResolvedValueOnce({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: 4,
        status: "pending",
        expires_at: "2026-07-25T10:00:00Z",
        files: [],
      })
    uploadBatchStatus.mockResolvedValue({
      batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
      status: "pending",
      applied_count: 0,
      files: [],
    })

    render(
      <I18nextProvider i18n={createI18n("en")}>
        <AudioManagementPage episodes={[]} canPublish locale="en" />
      </I18nextProvider>
    )
    fireEvent.change(screen.getByLabelText("Trading date"), {
      target: { value: "2026-07-25" },
    })
    const emptySlots = screen.getAllByLabelText(
      "Click to choose or drop an audio file here"
    )
    selectFile(
      emptySlots[2]!,
      new File(["old"], "old.mp3", { type: "audio/mpeg" })
    )
    const submit = screen.getByRole("button", { name: "Upload audio" })
    fireEvent.click(submit)
    await screen.findByRole("button", { name: "Confirm overwrite" })

    selectFile(
      screen.getByLabelText("old.mp3"),
      new File(["new"], "new.mp3", { type: "audio/mpeg" })
    )
    selectFile(
      screen.getAllByLabelText(
        "Click to choose or drop an audio file here"
      )[0]!,
      new File(["localized"], "hant.mp3", { type: "audio/mpeg" })
    )
    expect(
      screen.queryByRole("button", { name: "Confirm overwrite" })
    ).not.toBeInTheDocument()
    fireEvent.click(submit)
    await screen.findByRole("button", { name: "Confirm overwrite" })
    fireEvent.click(screen.getByRole("button", { name: "Confirm overwrite" }))

    await waitFor(() => expect(initializeUploadBatch).toHaveBeenCalledTimes(3))
    expect(initializeUploadBatch.mock.calls[2]?.[0].files).toMatchObject([
      { locale: "zh-hant", filename: "hant.mp3" },
      {
        locale: "en",
        filename: "new.mp3",
        confirm_replacement: true,
        expected_current_version: 5,
      },
    ])
  })

  it("keeps replacement confirmation scoped to the retried locale", async () => {
    const originalXhr = globalThis.XMLHttpRequest
    globalThis.XMLHttpRequest =
      MockUploadRequest as unknown as typeof XMLHttpRequest
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    const batchId = "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09"
    const enSession = "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11"
    const zhSession = "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22"
    initializeUploadBatch
      .mockResolvedValueOnce({
        batch_id: batchId,
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: null,
        status: "pending",
        expires_at: "2999-01-01T00:00:00Z",
        files: [
          {
            session_id: zhSession,
            asset_id: "3b6f1e4f-6c3d-4e62-9a3f-7a1e4b0d2c33",
            locale: "zh-hant",
            object_key: "audio/zh-hant.mp3",
            upload_url: "https://storage.example/zh-hant",
            required_headers: {
              "Content-Type": "audio/mpeg",
              "If-None-Match": "*",
              "x-amz-meta-sha256": "0".repeat(64),
            },
            expires_at: "2999-01-01T00:00:00Z",
            status: "pending_upload",
            request_id: "request-id",
          },
          {
            session_id: enSession,
            asset_id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
            locale: "en",
            object_key: "audio/en.mp3",
            upload_url: "https://storage.example/en",
            required_headers: {
              "Content-Type": "audio/mpeg",
              "If-None-Match": "*",
              "x-amz-meta-sha256": "0".repeat(64),
            },
            expires_at: "2999-01-01T00:00:00Z",
            status: "pending_upload",
            request_id: "request-id",
          },
        ],
      })
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Replacement required", {
          code: "replacement_confirmation_required",
          current_versions: { en: 7 },
        })
      )
      .mockResolvedValueOnce({
        batch_id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: 7,
        status: "pending",
        expires_at: "2999-01-01T00:00:00Z",
        files: [],
      })
    finalizeUploadBatchFile.mockResolvedValue({ status: "queued" })
    let siblingStatus = "processing"
    uploadBatchStatus.mockImplementation(() =>
      Promise.resolve({
        batch_id: batchId,
        status: siblingStatus === "failed" ? "failed" : "partial",
        applied_count: 0,
        files: [
          {
            session_id: zhSession,
            asset_id: "3b6f1e4f-6c3d-4e62-9a3f-7a1e4b0d2c33",
            locale: "zh-hant",
            status: siblingStatus,
            error_code: siblingStatus === "failed" ? "media_invalid" : null,
            sha256: null,
            duration_seconds: null,
          },
          {
            session_id: enSession,
            asset_id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
            locale: "en",
            status: "failed",
            error_code: "media_invalid",
            sha256: null,
            duration_seconds: null,
          },
        ],
      })
    )

    try {
      render(
        <I18nextProvider i18n={createI18n("en")}>
          <AudioManagementPage episodes={[]} canPublish locale="en" />
        </I18nextProvider>
      )
      fireEvent.change(screen.getByLabelText("Trading date"), {
        target: { value: "2026-07-25" },
      })
      const slots = screen.getAllByLabelText(
        "Click to choose or drop an audio file here"
      )
      selectFile(
        slots[0]!,
        new File(["hant"], "hant.mp3", { type: "audio/mpeg" })
      )
      selectFile(
        slots[2]!,
        new File(["english"], "english.mp3", { type: "audio/mpeg" })
      )
      fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))
      await screen.findByText(
        "Other language files are still processing. Retry after they finish.",
        undefined,
        { timeout: 3_000 }
      )
      const blockedRetry = screen.getByRole("button", { name: "Retry" })
      expect(blockedRetry).toBeDisabled()
      expect(initializeUploadBatch).toHaveBeenCalledTimes(1)

      siblingStatus = "failed"
      fireEvent.click(screen.getByRole("button", { name: "Refresh status" }))
      await waitFor(() =>
        expect(screen.getByText("Batch status: Failed")).toBeInTheDocument()
      )
      await waitFor(() =>
        expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(2)
      )

      expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(2)
      await waitFor(() => {
        for (const retryButton of screen.getAllByRole("button", {
          name: "Retry",
        })) {
          expect(retryButton).toBeEnabled()
        }
      })
      expect(initializeUploadBatch).toHaveBeenCalledTimes(1)

      fireEvent.click(screen.getAllByRole("button", { name: "Retry" })[1]!)
      await screen.findByRole("button", { name: "Confirm overwrite" })
      fireEvent.click(screen.getByRole("button", { name: "Confirm overwrite" }))

      await waitFor(() =>
        expect(initializeUploadBatch).toHaveBeenCalledTimes(3)
      )
      expect(initializeUploadBatch.mock.calls[2]?.[0].files).toEqual([
        expect.objectContaining({
          locale: "en",
          filename: "english.mp3",
          confirm_replacement: true,
          expected_current_version: 7,
        }),
      ])
      expect(screen.getByText("english.mp3")).toBeInTheDocument()
      expect(screen.getByText("hant.mp3")).toBeInTheDocument()
    } finally {
      globalThis.XMLHttpRequest = originalXhr
    }
  })

  it("resumes pending uploads after an interrupted partial batch", async () => {
    const originalXhr = globalThis.XMLHttpRequest
    globalThis.XMLHttpRequest =
      MockUploadRequest as unknown as typeof XMLHttpRequest
    MockUploadRequest.statusQueue = [200, 500, 500, 200]
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    const batchId = "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09"
    const sessionIds = {
      "zh-hant": "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11",
      "zh-hans": "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22",
      en: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
    }
    const assetIds = {
      "zh-hant": "3b6f1e4f-6c3d-4e62-9a3f-7a1e4b0d2c33",
      "zh-hans": "4c7a2f5e-7d4e-4f73-8b4f-8b2f5c1e3d44",
      en: "5d8b3f6a-8e5f-4a84-9c5f-9c3f6d2e4e55",
    }
    const uploadFiles = ["zh-hant", "zh-hans", "en"].map(locale => ({
      session_id: sessionIds[locale as keyof typeof sessionIds],
      asset_id: assetIds[locale as keyof typeof assetIds],
      locale,
      object_key: `audio/${locale}.mp3`,
      upload_url: `https://storage.example/${locale}`,
      required_headers: {
        "Content-Type": "audio/mpeg",
        "If-None-Match": "*",
        "x-amz-meta-sha256": "0".repeat(64),
      },
      expires_at: "2999-01-01T00:00:00Z",
      status: "pending_upload",
      request_id: "request-id",
    }))
    initializeUploadBatch.mockResolvedValue({
      batch_id: batchId,
      trading_date: "2026-07-25",
      reason: "initial_upload",
      base_episode_version: null,
      status: "pending",
      expires_at: "2999-01-01T00:00:00Z",
      files: uploadFiles,
    })
    const finalizeCounts: Record<string, number> = {}
    let resumedEnglish = false
    uploadBatchStatus.mockImplementation(() =>
      Promise.resolve({
        batch_id: batchId,
        status: "partial",
        applied_count: 1,
        files: [
          ...(["zh-hant", "zh-hans", "en"] as const).map(locale => ({
            session_id: sessionIds[locale],
            asset_id: assetIds[locale],
            locale,
            status:
              locale === "zh-hant"
                ? "completed"
                : locale === "en" && resumedEnglish
                  ? "processing"
                  : "pending_upload",
            error_code: null,
            sha256: locale === "zh-hant" ? "0".repeat(64) : null,
            duration_seconds: locale === "zh-hant" ? 10 : null,
          })),
        ],
      })
    )
    finalizeUploadBatchFile.mockImplementation(
      async (_id: string, locale: string) => {
        finalizeCounts[locale] = (finalizeCounts[locale] ?? 0) + 1
        if (locale !== "zh-hant" && finalizeCounts[locale] === 1) {
          throw new ApiError(409, "request-id", "Object not uploaded", {
            code: "object_not_uploaded",
          })
        }
        if (locale === "en") resumedEnglish = true
        return { status: "queued" }
      }
    )

    try {
      render(
        <I18nextProvider i18n={createI18n("en")}>
          <AudioManagementPage episodes={[]} canPublish locale="en" />
        </I18nextProvider>
      )
      fireEvent.change(screen.getByLabelText("Trading date"), {
        target: { value: "2026-07-25" },
      })
      const slots = screen.getAllByLabelText(
        "Click to choose or drop an audio file here"
      )
      selectFile(
        slots[0]!,
        new File(["hant"], "hant.mp3", { type: "audio/mpeg" })
      )
      selectFile(
        slots[1]!,
        new File(["hans"], "hans.mp3", { type: "audio/mpeg" })
      )
      selectFile(
        slots[2]!,
        new File(["english"], "english.mp3", { type: "audio/mpeg" })
      )
      fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))

      expect(await screen.findAllByText("Waiting to upload")).toHaveLength(2)
      await waitFor(() =>
        expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(2)
      )
      await waitFor(
        () => {
          for (const retryButton of screen.getAllByRole("button", {
            name: "Retry",
          })) {
            expect(retryButton).toBeEnabled()
          }
        },
        { timeout: 7_000 }
      )
      expect(
        screen.getByRole("button", { name: "Upload audio" })
      ).toBeDisabled()
      expect(initializeUploadBatch).toHaveBeenCalledTimes(1)

      fireEvent.click(screen.getAllByRole("button", { name: "Retry" })[1]!)
      await waitFor(() =>
        expect(finalizeUploadBatchFile).toHaveBeenCalledWith(
          batchId,
          "en",
          "csrf"
        )
      )
      await waitFor(() =>
        expect(screen.getByText("Processing")).toBeInTheDocument()
      )
      expect(finalizeCounts.en).toBe(2)
      expect(finalizeCounts["zh-hans"]).toBe(1)
      expect(initializeUploadBatch).toHaveBeenCalledTimes(1)
      expect(screen.getByText("hans.mp3")).toBeInTheDocument()
      expect(screen.getByText("english.mp3")).toBeInTheDocument()
    } finally {
      globalThis.XMLHttpRequest = originalXhr
    }
  })

  it("retries one locale after all sibling upload sessions expire", async () => {
    const originalXhr = globalThis.XMLHttpRequest
    globalThis.XMLHttpRequest =
      MockUploadRequest as unknown as typeof XMLHttpRequest
    MockUploadRequest.statusQueue = [500, 500, 200]
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    const oldBatchId = "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09"
    const newBatchId = "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf"
    const sessionIds = {
      "zh-hant": "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11",
      en: "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22",
    }
    const assetIds = {
      "zh-hant": "3b6f1e4f-6c3d-4e62-9a3f-7a1e4b0d2c33",
      en: "4c7a2f5e-7d4e-4f73-8b4f-8b2f5c1e3d44",
    }
    const targets = (batchId: string, expiresAt: string) =>
      (["zh-hant", "en"] as const).map(locale => ({
        session_id: sessionIds[locale],
        asset_id: assetIds[locale],
        locale,
        object_key: `audio/${batchId}/${locale}.mp3`,
        upload_url: `https://storage.example/${batchId}/${locale}`,
        required_headers: {
          "Content-Type": "audio/mpeg",
          "If-None-Match": "*",
          "x-amz-meta-sha256": "0".repeat(64),
        },
        expires_at: expiresAt,
        status: "pending_upload",
        request_id: "request-id",
      }))
    initializeUploadBatch
      .mockResolvedValueOnce({
        batch_id: oldBatchId,
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: null,
        status: "pending",
        expires_at: "2020-01-01T00:00:00Z",
        files: targets(oldBatchId, "2020-01-01T00:00:00Z"),
      })
      .mockResolvedValueOnce({
        batch_id: newBatchId,
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: null,
        status: "pending",
        expires_at: "2999-01-01T00:00:00Z",
        files: [targets(newBatchId, "2999-01-01T00:00:00Z")[1]],
      })
    finalizeUploadBatchFile.mockImplementation(
      async (batchId: string, locale: string) => {
        if (batchId === oldBatchId) {
          throw new ApiError(409, "request-id", "Object not uploaded", {
            code: "object_not_uploaded",
          })
        }
        return { status: "queued", locale }
      }
    )
    uploadBatchStatus.mockImplementation((batchId: string) =>
      Promise.resolve({
        batch_id: batchId,
        status: batchId === oldBatchId ? "partial" : "pending",
        applied_count: 0,
        files:
          batchId === oldBatchId
            ? (["zh-hant", "en"] as const).map(locale => ({
                session_id: sessionIds[locale],
                asset_id: assetIds[locale],
                locale,
                status: "pending_upload",
                error_code: null,
                sha256: null,
                duration_seconds: null,
              }))
            : [
                {
                  session_id: sessionIds.en,
                  asset_id: assetIds.en,
                  locale: "en",
                  status: "processing",
                  error_code: null,
                  sha256: null,
                  duration_seconds: null,
                },
              ],
      })
    )

    try {
      render(
        <I18nextProvider i18n={createI18n("en")}>
          <AudioManagementPage episodes={[]} canPublish locale="en" />
        </I18nextProvider>
      )
      fireEvent.change(screen.getByLabelText("Trading date"), {
        target: { value: "2026-07-25" },
      })
      const date = screen.getByLabelText("Trading date")
      const reason = screen.getByLabelText("Reason for change")
      const slots = screen.getAllByLabelText(
        "Click to choose or drop an audio file here"
      )
      selectFile(
        slots[0]!,
        new File(["hant"], "hant.mp3", { type: "audio/mpeg" })
      )
      selectFile(
        slots[2]!,
        new File(["english"], "english.mp3", { type: "audio/mpeg" })
      )
      fireEvent.click(screen.getByRole("button", { name: "Upload audio" }))

      expect(await screen.findAllByText("Waiting to upload")).toHaveLength(2)
      expect(date).toBeDisabled()
      expect(reason).toBeDisabled()
      await waitFor(
        () => {
          for (const retryButton of screen.getAllByRole("button", {
            name: "Retry",
          })) {
            expect(retryButton).toBeEnabled()
          }
        },
        { timeout: 7_000 }
      )
      expect(
        screen.getByRole("button", { name: "Upload audio" })
      ).toBeDisabled()

      fireEvent.click(screen.getAllByRole("button", { name: "Retry" })[1]!)
      await waitFor(() =>
        expect(initializeUploadBatch).toHaveBeenCalledTimes(2)
      )
      expect(initializeUploadBatch.mock.calls[1]?.[0]).toMatchObject({
        trading_date: "2026-07-25",
        reason: "initial_upload",
        files: [
          {
            locale: "en",
            filename: "english.mp3",
            sha256: "0".repeat(64),
          },
        ],
      })
      await screen.findByText("Processing")
      expect(screen.getByText("hant.mp3")).toBeInTheDocument()
      expect(screen.getByText("english.mp3")).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled()
    } finally {
      globalThis.XMLHttpRequest = originalXhr
    }
  }, 12_000)

  it("clears an expired idempotency key and keeps the selected file for retry", async () => {
    vi.spyOn(globalThis.crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).buffer
    )
    initializeUploadBatch
      .mockRejectedValueOnce(
        new ApiError(409, "request-id", "Expired", {
          code: "upload_batch_expired",
        })
      )
      .mockResolvedValueOnce({
        batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        trading_date: "2026-07-25",
        reason: "initial_upload",
        base_episode_version: null,
        status: "pending",
        expires_at: "2026-07-25T10:00:00Z",
        files: [],
      })
    uploadBatchStatus.mockResolvedValue({
      batch_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
      status: "pending",
      applied_count: 0,
      files: [],
    })

    render(
      <I18nextProvider i18n={createI18n("en")}>
        <AudioManagementPage episodes={[]} canPublish locale="en" />
      </I18nextProvider>
    )
    fireEvent.change(screen.getByLabelText("Trading date"), {
      target: { value: "2026-07-25" },
    })
    const audioFile = new File(["audio"], "episode.mp3", {
      type: "audio/mpeg",
    })
    fireEvent.change(
      screen.getAllByLabelText(
        "Click to choose or drop an audio file here"
      )[2]!,
      {
        target: {
          files: Object.assign([audioFile], {
            item(index: number) {
              return index === 0 ? audioFile : null
            },
          }),
        },
      }
    )

    const submit = screen.getByRole("button", { name: "Upload audio" })
    fireEvent.click(submit)
    await screen.findByText(
      "The upload session expired. Submit the retained files again."
    )
    expect(screen.getByText("episode.mp3")).toBeInTheDocument()
    fireEvent.click(submit)

    await waitFor(() => expect(initializeUploadBatch).toHaveBeenCalledTimes(2))
    expect(initializeUploadBatch.mock.calls[0]?.[0].idempotency_key).not.toBe(
      initializeUploadBatch.mock.calls[1]?.[0].idempotency_key
    )
    expect(screen.getByText("episode.mp3")).toBeInTheDocument()
  })
})
