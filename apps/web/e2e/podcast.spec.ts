import { expect, test } from "@playwright/test"
import {
  authenticateAs,
  episodeId,
  getMockApiState,
  openHydrated,
  resetMockApi,
} from "./helpers"

test.beforeEach(async ({ context, request }) => {
  await resetMockApi(request)
  await authenticateAs(context, "admin")
})

test.describe("Podcast administration", () => {
  test("uploads through language slots and confirms a replacement", async ({
    page,
    request,
  }) => {
    await openHydrated(
      page,
      "/en/back-office/podcasts",
      "#podcast-file-zh-hant"
    )

    const uploadForm = page
      .getByRole("heading", { name: "Upload Podcast" })
      .locator("..")
    await expect(uploadForm.locator('input[type="file"]')).toHaveCount(3)
    await uploadForm.getByLabel("Trading date").fill("2026-07-24")
    await uploadForm.locator("#podcast-file-zh-hant").setInputFiles({
      name: "briefing.mp3",
      mimeType: "audio/mpeg",
      buffer: Buffer.from("e2e audio"),
    })
    await uploadForm.getByLabel("Reason for change").selectOption("update_file")
    await uploadForm.getByRole("button", { name: "Upload audio" }).click()

    const warning = page.getByRole("alert")
    await expect(warning).toContainText("zh-hant")
    const confirm = warning.getByRole("button", { name: "Confirm overwrite" })
    await confirm.focus()
    await expect(confirm).toBeFocused()
    await confirm.press("Enter")

    await expect(page.getByText("Version 3")).toBeVisible()
    await expect(warning).toBeHidden()

    const uploadRequests = (await getMockApiState(request)).requests.filter(
      item => item.path === "/api/admin/podcasts/uploads"
    )
    expect(uploadRequests).toHaveLength(2)
    expect(uploadRequests[0]).toMatchObject({
      role: "admin",
      facts: {
        csrf: "valid",
        tradingDate: "2026-07-24",
        reason: "update_file",
        confirmReplacement: false,
        expectedVersions: {},
        files: {
          zh_hant: {
            filename: "briefing.mp3",
            contentType: "audio/mpeg",
            size: 9,
          },
        },
      },
    })
    expect(uploadRequests[1]).toMatchObject({
      role: "admin",
      facts: {
        csrf: "valid",
        confirmReplacement: true,
        expectedVersions: { "zh-hant": 1 },
      },
    })
  })

  test("requires confirmation before unpublishing", async ({
    page,
    request,
  }) => {
    await openHydrated(
      page,
      "/en/back-office/podcasts",
      ".podcast-publication-controls button"
    )
    const unpublish = page.getByRole("button", { name: "Unpublish" })

    page.once("dialog", dialog => dialog.dismiss())
    await unpublish.click()
    await expect(page.getByText("Published", { exact: true })).toBeVisible()

    page.once("dialog", dialog => dialog.accept())
    await unpublish.click()
    await expect(page.getByText("Draft", { exact: true })).toBeVisible()

    const unpublishRequests = (await getMockApiState(request)).requests.filter(
      item => item.path.endsWith("/unpublish")
    )
    expect(unpublishRequests).toEqual([
      expect.objectContaining({
        role: "admin",
        facts: { csrf: "valid", expectedVersion: 2 },
      }),
    ])
  })
})

test.describe("Customer Podcast experience", () => {
  test.beforeEach(async ({ context }) => {
    await context.clearCookies()
    await authenticateAs(context, "org_member")
  })

  test("opens list, detail, fallback player, and works at mobile size", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto("/en/podcasts")

    await expect(
      page.getByRole("heading", { name: "Market Morning Brief" })
    ).toBeVisible()
    const listen = page.getByRole("link", { name: /Market Morning Brief/ })
    await listen.focus()
    await expect(listen).toBeFocused()
    await listen.press("Enter")

    await expect(page).toHaveURL(`/en/podcasts/${episodeId}`)
    await expect(
      page.getByText(
        "Audio is not yet available in this language. Playing another available edition."
      )
    ).toBeVisible()
    await expect(page.locator("audio")).toHaveAttribute(
      "src",
      /\/media\/podcast\.wav$/
    )
    const layout = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }))
    expect(layout.content).toBeLessThanOrEqual(layout.viewport)
  })

  test("shows loading and unavailable player states", async ({
    page,
    request,
  }) => {
    await resetMockApi(request, { audio: "delayed" })
    await page.goto(`/en/podcasts/${episodeId}`)
    await expect(page.getByRole("status")).toHaveText("Preparing audio…")
    await expect(page.locator("audio")).toBeVisible()

    await resetMockApi(request, { audio: "error" })
    await page.reload()
    await expect(page.getByRole("alert")).toHaveText(
      "This audio is currently unavailable. Please try again later."
    )
  })

  test("shows the empty list state", async ({ page, request }) => {
    await resetMockApi(request, { podcastList: "empty" })
    await page.goto("/en/podcasts")
    await expect(
      page.getByRole("heading", { name: "Nothing to listen to yet" })
    ).toBeVisible()
  })

  test("shows an actionable loader error state", async ({ page, request }) => {
    await resetMockApi(request, { podcastList: "error" })
    await page.goto("/en/podcasts")
    const alert = page.getByRole("alert")
    await expect(alert).toContainText("The service is temporarily unavailable.")
    await expect(alert.getByRole("button", { name: "Retry" })).toBeVisible()
  })

  test("shows the detail error state for an unknown episode", async ({
    page,
  }) => {
    await page.goto("/en/podcasts/90000000-0000-4000-8000-000000000009")
    const alert = page.getByRole("alert")
    await expect(alert).toContainText("The service is temporarily unavailable.")
    await expect(alert).toContainText("Request ID: e2e-request-id")
  })
})
