import { expect, test } from "@playwright/test"
import {
  authenticateAs,
  customerCredentials,
  openHydrated,
  resetMockApi,
} from "./helpers"

test.beforeEach(async ({ context, request }) => {
  await resetMockApi(request)
  await context.clearCookies()
})

test("customer login opens reports, then a market detail without mobile overflow", async ({
  page,
}) => {
  await openHydrated(page, "/en/login", 'input[name="email"]')
  await page.getByLabel("Email").fill(customerCredentials.email)
  await page.getByLabel("Password").fill(customerCredentials.password)
  await page.getByRole("button", { name: "Sign in" }).click()
  await expect(page).toHaveURL("/en/reports")
  await page
    .getByRole("navigation", { name: "Market category navigation" })
    .getByRole("link", { name: "Macro, bonds & FX" })
    .click()
  await expect(page).toHaveURL("/en/reports/global_macro_bonds")
  await expect(
    page.getByRole("heading", { name: "Global macro, bonds & FX", level: 1 })
  ).toBeVisible()
  await expect(
    page.getByRole("heading", {
      name: "Global foreign exchange price trends",
    })
  ).toBeVisible()
  const calendarPanel = page
    .getByRole("heading", {
      name: "Today’s economic calendar + central bank events",
    })
    .locator("..")
  await expect(calendarPanel).toContainText(
    "Nasdaq · Sep 4, 2026 · Taipei time"
  )
  const fxPanel = page
    .getByRole("heading", { name: "Global foreign exchange price trends" })
    .locator("..")
  await page.getByLabel("Currency pair").selectOption("usd_jpy")
  await expect(page.getByLabel("Currency pair")).toHaveValue("usd_jpy")
  await expect(
    fxPanel.getByLabel("Latest observations and dates")
  ).toContainText("JPY")
  await page.getByRole("button", { name: "365 days" }).click()
  await expect(page.getByRole("button", { name: "365 days" })).toHaveAttribute(
    "aria-pressed",
    "true"
  )
  await page.setViewportSize({ width: 375, height: 720 })
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true)
})

test("unlaunched market has a clear non-error state", async ({
  context,
  page,
}) => {
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/tw_index_derivatives")
  await expect(
    page.getByRole("heading", { name: "Report not launched yet" })
  ).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("visible market without a publication shows a non-error state", async ({
  context,
  page,
  request,
}) => {
  await resetMockApi(request, { reports: "not_generated" })
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/us_equity")

  await expect(
    page.getByText("This section has not been generated yet.", { exact: true })
  ).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await expect(
    page.getByRole("navigation", { name: "Market category navigation" })
  ).toBeVisible()
})
