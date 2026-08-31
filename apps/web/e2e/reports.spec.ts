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
  await expect(
    page.getByText(
      "Official data is supplied by Twelve Data; gaps are shown explicitly at block level."
    )
  ).toBeVisible()
  await page.getByRole("link", { name: "View details" }).first().click()
  await expect(page).toHaveURL("/en/reports/global_macro_bonds")
  await expect(
    page.getByRole("heading", { name: "Global macro & bonds" })
  ).toBeVisible()
  await page.setViewportSize({ width: 375, height: 720 })
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true)
})

test("unavailable report preserves gap and block states", async ({
  context,
  page,
}) => {
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/tw_index_derivatives")
  await expect(
    page.getByText(/Not launched \/ illustrative data/)
  ).toBeVisible()
  await expect(page.getByText("Unavailable", { exact: true })).toBeVisible()
  await expect(page.getByText("Data missing", { exact: true })).toHaveCount(3)
  await expect(page.getByText("Data error", { exact: true })).toHaveCount(1)
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
    page.getByRole("heading", { name: "Morning report not generated yet" })
  ).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await expect(
    page.getByRole("navigation", { name: "Market category navigation" })
  ).toBeVisible()
})
