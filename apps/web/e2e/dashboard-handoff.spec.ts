import { expect, test } from "@playwright/test"
import { authenticateAs, getMockApiState, resetMockApi } from "./helpers"

const locales = ["zh-hant", "zh-hans", "en"] as const
const technical = {
  "zh-hant": "台股乖離率",
  "zh-hans": "台股乖离率",
  en: "TAIEX bias",
}
const macro = {
  "zh-hant": "重點指標",
  "zh-hans": "重点指标",
  en: "Key figures",
}
for (const locale of locales) {
  for (const market of ["global_macro_bonds", "tw_equity"] as const) {
    test(`${locale} ${market}: desktop layout, locale tokens and screenshot`, async ({
      page,
      context,
      request,
    }, info) => {
      await resetMockApi(request)
      await authenticateAs(context, "org_member")
      const errors: string[] = []
      page.on("pageerror", error => errors.push(error.message))
      await page.setViewportSize({ width: 1440, height: 1100 })
      await page.goto(`/${locale}/reports/${market}`)
      await expect(
        page.getByRole("heading", {
          name: market === "tw_equity" ? technical[locale] : macro[locale],
          exact: true,
        })
      ).toBeVisible()
      const count = market === "tw_equity" ? 2 : 4
      await expect(page.locator("canvas")).toHaveCount(count, {
        timeout: 15_000,
      })
      if (market === "tw_equity")
        await expect(page.getByRole("meter").first()).toHaveAttribute(
          "aria-valuenow",
          /\d/
        )
      await page.evaluate(() => document.fonts.ready)
      const appearance = await page.evaluate(() => {
        const root = getComputedStyle(document.documentElement)
        return {
          font: getComputedStyle(document.body).fontFamily,
          up: root.getPropertyValue("--market-up").trim(),
          down: root.getPropertyValue("--market-down").trim(),
        }
      })
      expect(
        appearance.font.startsWith(
          locale === "en"
            ? '"Noto Sans",'
            : locale === "zh-hans"
              ? '"Noto Sans SC",'
              : '"Noto Sans TC",'
        )
      ).toBe(true)
      expect(appearance.up).toBe(locale === "en" ? "#2f9e69" : "#d6453f")
      expect(appearance.down).toBe(locale === "en" ? "#d6453f" : "#2f9e69")
      if (locale === "en" && market === "global_macro_bonds") {
        await expect(
          page.getByRole("heading", {
            name: "Today’s economic calendar + central bank events",
          })
        ).toBeVisible()
        expect(await page.locator("body").innerText()).not.toContain("Today’ s")
        const apostropheWidth = await page.evaluate(() => {
          const canvas = document.createElement("canvas")
          const context = canvas.getContext("2d")!
          context.font = `16px ${getComputedStyle(document.body).fontFamily}`
          return context.measureText("’").width
        })
        expect(apostropheWidth).toBeLessThan(8)
      }
      await page.screenshot({
        path: info.outputPath(`${locale}-${market}.png`),
        fullPage: true,
      })
      for (const width of [1280, 1024]) {
        await page.setViewportSize({ width, height: 1100 })
        await expect
          .poll(() =>
            page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth
            )
          )
          .toBe(true)
        // Text must fit its own table cell; clipping the page does not count as fitting.
        const overflow = await page
          .locator("table:visible th, table:visible td")
          .evaluateAll(cells =>
            cells
              .filter(
                cell =>
                  !cell.closest(".sr-only") &&
                  cell.scrollWidth > cell.clientWidth + 1
              )
              .map(cell => cell.textContent)
          )
        expect(overflow).toEqual([])
        await page.screenshot({
          path: info.outputPath(`${locale}-${market}-${width}.png`),
          fullPage: true,
        })
      }
      expect(errors).toEqual([])
    })
  }
}

test("technical controls and chart zoom only slice the already loaded data", async ({
  page,
  context,
  request,
}) => {
  await resetMockApi(request)
  await authenticateAs(context, "org_member")
  await page.setViewportSize({ width: 1440, height: 1100 })
  await page.goto("/en/reports/tw_equity")
  const meter = page.getByRole("meter", { name: "20MA bias", exact: true })
  await expect(meter).toHaveAttribute("aria-valuenow", /\d/)
  const requestsBefore = (await getMockApiState(request)).requests.filter(r =>
    r.path.includes("/indices/")
  ).length
  const before = await meter.getAttribute("aria-valuenow")
  await page.getByRole("button", { name: "Last 1 year", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "Last 1 year", exact: true })
  ).toHaveAttribute("aria-pressed", "true")
  await page.getByRole("button", { name: "Last 2 years", exact: true }).click()
  await expect.poll(() => meter.getAttribute("aria-valuenow")).not.toBe(before)
  const chart = page
    .getByRole("img", { name: "TAIEX bias", exact: true })
    .first()
  await chart.scrollIntoViewIfNeeded()
  const box = await chart.boundingBox()
  if (!box) throw new Error("Bias chart has no bounds")
  const valueBeforeZoom = await meter.getAttribute("aria-valuenow")
  // Drag the actual slider's right handle to exercise ECharts' datazoom event.
  await page.mouse.move(box.x + box.width - 21, box.y + box.height - 9)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height - 9, {
    steps: 12,
  })
  await page.mouse.up()
  await expect
    .poll(() => meter.getAttribute("aria-valuenow"))
    .not.toBe(valueBeforeZoom)
  const requestsAfter = (await getMockApiState(request)).requests.filter(r =>
    r.path.includes("/indices/")
  ).length
  expect(requestsAfter).toBe(requestsBefore)
})
