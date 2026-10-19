import { expect, test, type Locator } from "@playwright/test"
import { authenticateAs, resetMockApi } from "./helpers"

// Inspect the rendered pixels, rather than only the configured matrix insets.
// MatrixView aligns a 1px stroke to the positive half-pixel; without a right
// inset its entire right edge is outside the canvas, at width + 0.5.
async function expectCompleteFrame(canvas: Locator) {
  await expect(canvas).toBeVisible({ timeout: 15_000 })
  await expect
    .poll(() =>
      canvas.evaluate(element => {
        const canvas = element as HTMLCanvasElement
        const context = canvas.getContext("2d")!
        const scale = canvas.width / canvas.clientWidth
        const width = canvas.clientWidth
        const bottom = canvas.clientHeight - 54
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height)
        const alpha = (x: number, y: number) =>
          pixels.data[
            (Math.floor(y * scale) * canvas.width + Math.floor(x * scale)) * 4 +
              3
          ]!
        const vertical = (x: number) => {
          for (let y = 6; y < bottom; y++) {
            if (alpha(x, y) < 200) return false
          }
          return true
        }
        const horizontal = (y: number) => {
          for (let x = 2; x < width - 1; x++) {
            if (alpha(x, y) < 200) return false
          }
          return true
        }
        return {
          left: vertical(1.5),
          right: vertical(width - 0.5),
          top: horizontal(4.5),
          bottom: horizontal(bottom + 0.5),
        }
      })
    )
    .toEqual({ left: true, right: true, top: true, bottom: true })
}

for (const deviceScaleFactor of [1, 2]) {
  test.describe(`matrix frame DPR ${deviceScaleFactor}`, () => {
    test.use({ deviceScaleFactor })
    test("all chart frames survive mobile resizing and theme changes", async ({
      page,
      context,
      request,
    }, info) => {
      await resetMockApi(request)
      await authenticateAs(context, "org_member")
      const errors: string[] = []
      page.on("pageerror", error => errors.push(error.message))
      for (const market of ["us_equity", "tw_equity"]) {
        await page.setViewportSize({ width: 1440, height: 1100 })
        await page.goto(`/en/reports/${market}`)
        const charts =
          market === "us_equity"
            ? [
                page.locator('[aria-labelledby="index-history-title"] canvas'),
                page.locator('[aria-labelledby="vix-history-title"] canvas'),
              ]
            : [
                page
                  .getByRole("img", {
                    name: "Taiwan Weighted Index",
                    exact: true,
                  })
                  .first()
                  .locator("canvas"),
              ]
        for (const width of [1440, 390, 1024]) {
          await page.setViewportSize({ width, height: 1100 })
          for (const chart of charts) {
            await expectCompleteFrame(chart)
          }
        }
        for (const mode of ["Dark", "Light"]) {
          await page
            .getByRole("button", { name: "Settings", exact: true })
            .click()
          await page
            .getByRole("radio", { name: mode, exact: true })
            .locator("..")
            .click()
          await page.keyboard.press("Escape")
          await expect(page.locator("html")).toHaveClass(
            mode === "Dark" ? /dark/ : /light/
          )
          for (const chart of charts) await expectCompleteFrame(chart)
        }
        await charts[0]!.screenshot({
          path: info.outputPath(`${market}-frame.png`),
        })
        if (deviceScaleFactor === 1 && market === "us_equity") {
          await charts[0]!.screenshot({
            path: "/tmp/chart-matrix-frame-fixed.png",
          })
        }
      }
      expect(errors).toEqual([])
    })
  })
}
