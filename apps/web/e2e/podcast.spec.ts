import { expect, test, type Page } from "@playwright/test"
import {
  adminCredentials,
  authenticateAs,
  customerCredentials,
  episodeId,
  getMockApiState,
  openHydrated,
  resetMockApi,
} from "./helpers"

test.beforeEach(async ({ context, request }) => {
  await resetMockApi(request)
  await context.clearCookies()
})

async function signIn(
  page: Page,
  path: string,
  credentials: { email: string; password: string }
) {
  await openHydrated(page, path, 'input[name="email"]')
  await page.getByLabel("Email").fill(credentials.email)
  await page.getByLabel("Password").fill(credentials.password)
  await page.getByRole("button", { name: "Sign in" }).click()
}

test.describe("Portal authentication and boundaries", () => {
  test("customer form login enters the Podcast surface", async ({
    page,
    request,
  }) => {
    await signIn(page, "/en/login", customerCredentials)

    await expect(page).toHaveURL("/en/podcasts")
    await expect(page.getByRole("heading", { name: "Podcast" })).toBeVisible()
    await expect(page.locator('[data-surface="customer"]')).toBeVisible()
    const login = (await getMockApiState(request)).requests.find(
      item => item.path === "/api/auth/login"
    )
    expect(login).toMatchObject({
      role: null,
      facts: {
        email: customerCredentials.email,
        credentialAccepted: true,
        authenticatedRole: "org_member",
      },
    })
  })

  test("admin form login enters audio management", async ({
    page,
    request,
  }) => {
    await signIn(page, "/en/admin/login", adminCredentials)

    await expect(page).toHaveURL("/en/admin/audio")
    await expect(
      page.getByRole("heading", { name: "Audio management" })
    ).toBeVisible()
    await expect(page.locator('[data-surface="admin"]')).toBeVisible()
    const login = (await getMockApiState(request)).requests.find(
      item => item.path === "/api/auth/login"
    )
    expect(login).toMatchObject({
      role: null,
      facts: {
        email: adminCredentials.email,
        credentialAccepted: true,
        authenticatedRole: "admin",
      },
    })
  })

  test("wrong-role portal login clears the session and localizes the mismatch", async ({
    context,
    page,
    request,
  }) => {
    await openHydrated(page, "/zh-hant/login", 'input[name="email"]')
    await page.getByLabel("電子郵件").fill(adminCredentials.email)
    await page.getByLabel("密碼").fill(adminCredentials.password)
    await page.getByRole("button", { name: "登入" }).click()

    await expect(page).toHaveURL("/zh-hant/login")
    await expect(page.getByRole("alert")).toHaveText(
      "此帳號目前無法登入，登入狀態已安全清除。"
    )
    expect(
      (await context.cookies()).find(cookie => cookie.name === "e2e-role")
    ).toBeUndefined()
    const authRequests = (await getMockApiState(request)).requests.filter(
      item => ["/api/auth/login", "/api/auth/logout"].includes(item.path)
    )
    expect(authRequests).toEqual([
      expect.objectContaining({
        path: "/api/auth/login",
        facts: expect.objectContaining({ authenticatedRole: "admin" }),
      }),
      expect.objectContaining({
        path: "/api/auth/logout",
        role: "admin",
        facts: { csrf: "valid" },
      }),
    ])
  })

  test("unauthenticated protected URLs reach their respective login portals", async ({
    page,
  }) => {
    await page.goto("/en/podcasts")
    await expect(page).toHaveURL("/en/login")
    await expect(
      page.getByRole("heading", {
        name: "Your market briefing, ready to listen",
      })
    ).toBeVisible()

    await page.goto("/en/admin/audio")
    await expect(page).toHaveURL("/en/admin/login")
    await expect(
      page.getByRole("heading", { name: "Audio management sign in" })
    ).toBeVisible()
  })

  test("authenticated cross-boundary access resolves to the allowed surface", async ({
    context,
    page,
  }) => {
    await authenticateAs(context, "admin")
    await page.goto("/en/podcasts")
    await expect(page).toHaveURL("/en/admin/audio")
    await expect(page.locator('[data-surface="admin"]')).toBeVisible()
    await expect(
      page.getByRole("heading", { name: "Market Morning Brief" })
    ).toHaveCount(0)

    await context.clearCookies()
    await authenticateAs(context, "org_member")
    await page.goto("/en/admin/audio")
    await expect(page).toHaveURL("/en/podcasts")
    await expect(page.locator('[data-surface="customer"]')).toBeVisible()
    await expect(
      page.getByRole("heading", { name: "Audio management" })
    ).toHaveCount(0)
  })

  test("logout returns each surface to its matching login", async ({
    context,
    page,
  }) => {
    await authenticateAs(context, "org_member")
    await openHydrated(
      page,
      "/en/podcasts",
      '[data-surface="customer"] button[type="button"]:last-of-type'
    )
    await page.getByRole("button", { name: "Sign out" }).click()
    await expect(page).toHaveURL("/en/login")

    await authenticateAs(context, "admin")
    await openHydrated(
      page,
      "/en/admin/audio",
      '[data-surface="admin"] button[type="button"]:last-of-type'
    )
    await page.getByRole("button", { name: "Sign out" }).click()
    await expect(page).toHaveURL("/en/admin/login")
  })
})

test.describe("Podcast administration", () => {
  test.beforeEach(async ({ context }) => {
    await authenticateAs(context, "admin")
  })

  test("uploads through language slots and confirms a replacement", async ({
    page,
    request,
  }) => {
    await openHydrated(page, "/en/admin/audio", "#podcast-file-zh-hant")

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
      "/en/admin/audio",
      'button[data-action="publication"]'
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

test.describe("Customer inline Podcast experience", () => {
  test.beforeEach(async ({ context }) => {
    await authenticateAs(context, "org_member")
  })

  test("plays inline with locale fallback at mobile width", async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto("/en/podcasts")

    await expect(
      page.getByRole("heading", { name: "Market Morning Brief" })
    ).toBeVisible()
    await expect(page).toHaveURL("/en/podcasts")
    await expect(
      page.getByRole("link", { name: /Market Morning Brief/ })
    ).toHaveCount(0)
    const player = page.getByRole("region", {
      name: "Market Morning Brief player",
    })
    expect(
      (await getMockApiState(request)).requests.filter(item =>
        item.path.endsWith("/audio-url")
      )
    ).toHaveLength(0)
    await player.getByRole("button", { name: "Listen now" }).click()
    await expect(player).toContainText(
      "Audio is not yet available in this language. Playing another available edition."
    )
    await expect(player.locator("audio")).toHaveAttribute(
      "src",
      /\/media\/podcast\.wav$/
    )
    expect(
      (await getMockApiState(request)).requests.filter(item =>
        item.path.endsWith("/audio-url")
      )
    ).toHaveLength(1)
    const layout = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }))
    expect(layout.content).toBeLessThanOrEqual(layout.viewport)
  })

  test("shows loading and unavailable inline player states", async ({
    page,
    request,
  }) => {
    await resetMockApi(request, { audio: "delayed" })
    await openHydrated(
      page,
      "/en/podcasts",
      '[data-testid="podcast-player"] button'
    )
    await page.getByRole("button", { name: "Listen now" }).click()
    await expect(page.getByRole("status")).toHaveText("Preparing audio…")
    await expect(page.locator("audio")).toBeVisible()

    await resetMockApi(request, { audio: "error" })
    await page.reload()
    await page.getByRole("button", { name: "Listen now" }).click()
    await expect(page.getByRole("alert")).toHaveText(
      "This audio is currently unavailable. Please try again later."
    )
    await expect(
      page.getByRole("button", { name: "Retry audio" })
    ).toBeVisible()
    expect(
      (await getMockApiState(request)).requests.filter(item =>
        item.path.endsWith("/audio-url")
      )
    ).toHaveLength(1)

    await resetMockApi(request)
    await page.getByRole("button", { name: "Retry audio" }).click()
    await expect(page.locator("audio")).toBeVisible()
    expect(
      (await getMockApiState(request)).requests.filter(item =>
        item.path.endsWith("/audio-url")
      )
    ).toHaveLength(1)
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

  test("redirects a legacy detail URL to the inline list", async ({ page }) => {
    await page.goto(`/en/podcasts/${episodeId}`)
    await expect(page).toHaveURL("/en/podcasts")
    await expect(
      page.getByRole("heading", { name: "Market Morning Brief" })
    ).toBeVisible()
    await expect(
      page.getByRole("region", { name: "Market Morning Brief player" })
    ).toBeVisible()
  })
})

test.describe("Customer account", () => {
  test.beforeEach(async ({ context }) => {
    await authenticateAs(context, "org_member")
  })

  test("shows the member profile without password controls", async ({
    page,
    request,
  }) => {
    await openHydrated(page, "/en/podcasts", '[data-surface="customer"]')
    await page.getByRole("link", { name: "Account" }).click()

    await expect(page).toHaveURL("/en/account")
    await expect(
      page.getByRole("heading", { name: "Account profile" })
    ).toBeVisible()
    await expect(
      page.getByRole("heading", { name: "E2E Member" })
    ).toBeVisible()
    await expect(page.getByText("org_member@example.test")).toBeVisible()
    await expect(page.getByLabel("Current password")).toHaveCount(0)
    await expect(page.getByLabel("New password")).toHaveCount(0)
    await expect(
      page.getByRole("button", { name: "Change password" })
    ).toHaveCount(0)

    await page.getByRole("button", { name: "Sign out" }).click()
    await expect(page).toHaveURL("/en/login")
    expect(
      (await getMockApiState(request)).requests.find(
        item => item.path === "/api/auth/logout"
      )
    ).toMatchObject({
      role: "org_member",
      facts: {
        csrf: "valid",
      },
    })
  })
})

test.describe("Mounted session expiry", () => {
  test("customer signing failure returns to customer login", async ({
    context,
    page,
    request,
  }) => {
    await authenticateAs(context, "org_member")
    await openHydrated(
      page,
      "/en/podcasts",
      '[data-testid="podcast-player"] button'
    )
    await resetMockApi(request, { sessionExpired: true })

    await page.getByRole("button", { name: "Listen now" }).click()

    await expect(page).toHaveURL("/en/login")
    await expect(
      page.getByRole("heading", {
        name: "Your market briefing, ready to listen",
      })
    ).toBeVisible()
    await expect(
      page.getByRole("heading", { name: "Market Morning Brief" })
    ).toHaveCount(0)
    expect(
      (await getMockApiState(request)).requests.some(
        item =>
          item.path.endsWith("/audio-url") &&
          item.facts?.sessionExpired === true
      )
    ).toBe(true)
  })

  test("admin managed action returns to admin login", async ({
    context,
    page,
    request,
  }) => {
    await authenticateAs(context, "admin")
    await openHydrated(
      page,
      "/en/admin/audio",
      'button[data-action="publication"]'
    )
    await resetMockApi(request, { sessionExpired: true })

    page.once("dialog", dialog => dialog.accept())
    await page.getByRole("button", { name: "Unpublish" }).click()

    await expect(page).toHaveURL("/en/admin/login")
    await expect(
      page.getByRole("heading", { name: "Audio management sign in" })
    ).toBeVisible()
    await expect(
      page.getByRole("heading", { name: "Audio management", exact: true })
    ).toHaveCount(0)
    expect(
      (await getMockApiState(request)).requests.some(
        item =>
          item.path === "/api/auth/csrf" && item.facts?.sessionExpired === true
      )
    ).toBe(true)
  })
})
