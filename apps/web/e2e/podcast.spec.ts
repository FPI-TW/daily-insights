import { expect, test, type Page } from "@playwright/test"
import {
  adminCredentials,
  assetManagerCredentials,
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

    await expect(page).toHaveURL("/en/reports")
    await expect(
      page.getByRole("heading", { name: "Market morning reports" })
    ).toBeVisible()
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

  test("both back-office roles can sign in to the customer portal", async ({
    context,
    page,
    request,
  }) => {
    for (const [credentials, role] of [
      [adminCredentials, "admin"],
      [assetManagerCredentials, "asset_manager"],
    ] as const) {
      await signIn(page, "/en/login", credentials)
      await expect(page).toHaveURL("/en/reports")
      await expect(page.locator('[data-surface="customer"]')).toBeVisible()
      await expect(
        page.getByRole("heading", { name: "Market morning reports" })
      ).toBeVisible()

      const login = [...(await getMockApiState(request)).requests]
        .reverse()
        .find(
          item =>
            item.path === "/api/auth/login" &&
            item.facts?.authenticatedRole === role
        )
      expect(login).toMatchObject({
        role: null,
        facts: {
          email: credentials.email,
          credentialAccepted: true,
          authenticatedRole: role,
        },
      })

      await context.clearCookies()
    }
  })

  test("unauthenticated protected URLs reach their respective login portals", async ({
    page,
  }) => {
    await page.goto("/en/podcasts")
    await expect(page).toHaveURL("/en/login")
    await expect(
      page.getByRole("heading", {
        name: "Your daily market reports and briefing",
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
    await expect(page).toHaveURL("/en/podcasts")
    await expect(page.locator('[data-surface="customer"]')).toBeVisible()

    await page.goto("/en/admin/audio")
    await expect(page).toHaveURL("/en/admin/audio")
    await expect(page.locator('[data-surface="admin"]')).toBeVisible()

    await context.clearCookies()
    await authenticateAs(context, "org_member")
    await page.goto("/en/admin/audio")
    await expect(page).toHaveURL("/en/reports")
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
    await resetMockApi(request, { status: "draft" })
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
    await expect(page.getByText("Draft", { exact: true })).toBeVisible()
    const confirm = warning.getByRole("button", { name: "Confirm overwrite" })
    await confirm.focus()
    await expect(confirm).toBeFocused()
    await confirm.press("Enter")

    await expect(page.getByText("Version 3")).toBeVisible()
    await expect(page.getByText("Published", { exact: true })).toBeVisible()
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

  test("lets an asset manager publish and unpublish", async ({
    context,
    page,
    request,
  }) => {
    await context.clearCookies()
    await authenticateAs(context, "asset_manager")
    await openHydrated(
      page,
      "/en/admin/audio",
      'button[data-action="publication"]'
    )

    const publication = page.locator('button[data-action="publication"]')
    await expect(publication).toHaveText("Unpublish")
    page.once("dialog", dialog => dialog.accept())
    await publication.click()
    await expect(publication).toHaveText("Publish")
    await publication.click()
    await expect(publication).toHaveText("Unpublish")

    const publicationRequests = (
      await getMockApiState(request)
    ).requests.filter(
      item => item.path.endsWith("/unpublish") || item.path.endsWith("/publish")
    )
    expect(publicationRequests).toEqual([
      expect.objectContaining({
        role: "asset_manager",
        facts: { csrf: "valid", expectedVersion: 2 },
      }),
      expect.objectContaining({
        role: "asset_manager",
        facts: { csrf: "valid", expectedVersion: 3 },
      }),
    ])
  })

  test("groups compact episode cards by localized descending month", async ({
    page,
    request,
  }) => {
    await resetMockApi(request, { podcastEpisodes: "grouped" })
    await openHydrated(page, "/en/admin/audio", "article")

    const monthHeadings = page.locator(
      'section[aria-labelledby^="audio-month-"] > h3'
    )
    await expect(monthHeadings).toHaveText([
      "July 2026",
      "June 2026",
      "May 2026",
    ])
    await expect(page.locator("article time")).toHaveText([
      "2026-07-26",
      "2026-07-24",
      "2026-06-30",
      "2026-05-02",
    ])

    const desktopLayout = await page.locator("article").evaluateAll(cards =>
      cards.map(card => {
        const bounds = card.getBoundingClientRect()
        return { left: bounds.left, width: bounds.width }
      })
    )
    expect(new Set(desktopLayout.map(card => card.left)).size).toBe(1)
    const listWidth = await page
      .locator("#audio-list-title")
      .locator("..")
      .evaluate(element => element.getBoundingClientRect().width)
    expect(desktopLayout[0]?.width).toBeLessThan(listWidth)

    await page.setViewportSize({ width: 390, height: 844 })
    const mobileWidths = await page.locator("article").evaluateAll(cards =>
      cards.map(card => ({
        card: card.getBoundingClientRect().width,
        parent: card.parentElement?.getBoundingClientRect().width ?? 0,
      }))
    )
    for (const widths of mobileWidths) {
      expect(Math.abs(widths.card - widths.parent)).toBeLessThan(1)
    }
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
    await openHydrated(
      page,
      "/en/podcasts",
      '[data-testid="podcast-player"] button'
    )

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

  test("shows the member profile with preferences and a password dialog", async ({
    page,
    request,
  }) => {
    await openHydrated(page, "/en/podcasts", '[data-surface="customer"]')
    await page.getByRole("link", { name: "Account" }).click()

    await expect(page).toHaveURL("/en/account")
    await expect(page.getByRole("heading", { name: "Account" })).toBeVisible()
    await expect(
      page.getByRole("heading", { name: "E2E Member" })
    ).toBeVisible()
    await expect(page.getByText("org_member@example.test")).toBeVisible()
    await expect(page.getByText("Organization member")).toBeVisible()
    await expect(
      page.getByRole("radiogroup", { name: "Appearance" })
    ).toBeVisible()
    await expect(page.getByLabel("Current password")).toHaveCount(0)

    await page.getByRole("button", { name: "Change password" }).click()
    const dialog = page.getByRole("dialog", { name: "Change password" })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByLabel("Current password")).toBeFocused()
    await dialog
      .getByLabel("Current password")
      .fill(customerCredentials.password)
    await dialog.getByLabel("New password").fill("short")
    await dialog.getByRole("button", { name: "Change password" }).click()
    await expect(dialog.getByLabel("New password")).toHaveAttribute(
      "aria-invalid",
      "true"
    )
    await dialog.getByLabel("New password").fill("new-password-1234")
    await dialog.getByRole("button", { name: "Change password" }).click()
    await expect(dialog).toBeHidden()
    await expect(
      page.getByText("Your password has been updated.")
    ).toBeVisible()
    expect(
      (await getMockApiState(request)).requests.find(
        item => item.path === "/api/auth/change-password"
      )
    ).toMatchObject({ role: "org_member" })

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
        name: "Your daily market reports and briefing",
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
