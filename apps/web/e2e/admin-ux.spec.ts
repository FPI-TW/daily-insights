import { expect, test, type Locator } from "@playwright/test"
import { authenticateAs, openHydrated, resetMockApi } from "./helpers"

async function documentPosition(locator: Locator) {
  return locator.evaluate(element => {
    const bounds = element.getBoundingClientRect()
    return { x: bounds.x + window.scrollX, y: bounds.y + window.scrollY }
  })
}

for (const width of [390, 768, 1280, 1440]) {
  test(`audio actions stay in place across publication hints at ${width}px`, async ({
    page,
    request,
    context,
  }) => {
    await resetMockApi(request, { status: "draft" })
    await authenticateAs(context, "admin")
    await page.setViewportSize({ width, height: 1000 })
    await openHydrated(page, "/en/admin/audio", "article")
    page.on("dialog", dialog => dialog.accept())
    const button = page.locator('[data-action="publication"]')
    const removal = page.locator('[data-action="removal"]')
    const before = await documentPosition(button)
    const removalBefore = await documentPosition(removal)
    await button.click()
    await expect(button).toHaveText("Unpublish")
    await expect(button).toBeEnabled()
    const after = await documentPosition(button)
    const removalAfter = await documentPosition(removal)
    expect(Math.abs(after!.y - before!.y)).toBeLessThan(1)
    expect(Math.abs(after!.x - before!.x)).toBeLessThan(1)
    expect(Math.abs(removalAfter!.y - removalBefore!.y)).toBeLessThan(1)
    await button.click()
    await expect(button).toHaveText("Publish")
    await expect(button).toBeEnabled()
    const reverted = await documentPosition(button)
    expect(Math.abs(reverted!.y - before!.y)).toBeLessThan(1)
    await page.getByRole("button", { name: "Edit localized content" }).click()
    await expect(page.locator("[data-metadata-editor]")).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth
      )
    ).toBe(true)
    await page.screenshot({
      path: `/tmp/admin-ux-audio-${width}.png`,
      fullPage: true,
    })
  })
}
