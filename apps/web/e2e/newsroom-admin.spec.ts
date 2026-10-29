import { expect, test } from "@playwright/test"
import { authenticateAs, getMockApiState, resetMockApi } from "./helpers"

const itemPath =
  "/api/admin/newsroom/items/72000000-0000-4000-8000-000000000001"
const eventPath =
  "/api/admin/newsroom/events/71000000-0000-4000-8000-000000000001"
const publishPath =
  "/api/admin/newsroom/editions/70000000-0000-4000-8000-000000000001/publish"

test("zh-hant: review the draft, edit its text, then approve it", async ({
  page,
  context,
  request,
}, testInfo) => {
  await resetMockApi(request)
  await authenticateAs(context, "admin")

  // Review: the Slack link lands on today's edition with every state shown.
  await page.goto("/zh-hant/admin/newsroom")
  await expect(
    page.getByRole("heading", { name: "重點新聞審核", level: 1 })
  ).toBeVisible({ timeout: 15_000 })
  await expect(
    page.getByRole("navigation", { name: "管理導覽" }).getByRole("link", {
      name: "重點新聞審核",
    })
  ).toHaveAttribute("aria-current", "page")
  const globalTab = page.getByRole("tab", { name: /全球/ })
  await expect(globalTab).toHaveAttribute("aria-selected", "true")
  await expect(globalTab.getByText("草稿")).toBeVisible()
  await expect(page.getByText(/自動發布/).first()).toBeVisible()
  await expect(page.getByText("尚有 3 篇文章未初篩")).toBeVisible()
  const card = page.getByRole("article", { name: "聯準會維持利率不變" })
  await expect(card.getByText("利率路徑牽動全球資金流向。")).toBeVisible()
  const blocked = page.getByRole("article", { name: "Chip export rules" })
  await expect(blocked.getByText("缺全文")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "將「Oil supply cut」加入此版" })
  ).toBeEnabled()

  // Edit: rewrite this market's "why" and the shared headline.
  await card.getByRole("button", { name: "改「為何重要」" }).click()
  const whyDialog = page.getByRole("dialog")
  await whyDialog.getByRole("textbox").fill("降息預期延後，資金回流美元資產。")
  await whyDialog.getByRole("button", { name: "儲存" }).click()
  await expect(whyDialog).toBeHidden()
  await expect(card.getByText("降息預期延後，資金回流美元資產。")).toBeVisible()

  await card.getByRole("button", { name: "改標題／摘要" }).click()
  const eventDialog = page.getByRole("dialog")
  await eventDialog.getByLabel("標題").fill("聯準會按兵不動")
  await eventDialog.getByRole("button", { name: "儲存" }).click()
  await expect(eventDialog).toBeHidden()
  const edited = page.getByRole("article", { name: "聯準會按兵不動" })
  await expect(edited).toBeVisible()

  // Approve: confirm, then the tab and the item actions switch to published.
  await page.getByRole("button", { name: "核准此市場" }).click()
  const confirm = page.getByRole("alertdialog")
  await expect(confirm).toContainText("核准全球草稿？")
  await confirm.getByRole("button", { name: "確認核准" }).click()
  await expect(confirm).toBeHidden()
  await expect(globalTab.getByText("已發布")).toBeVisible()
  await expect(page.getByText(/由管理員核准發布/)).toBeVisible()
  await expect(edited.getByRole("button", { name: "隱藏" })).toBeVisible()
  await expect(edited.getByRole("button", { name: "移除" })).toHaveCount(0)

  const writes = (await getMockApiState(request)).requests.filter(
    entry => entry.method !== "GET" && entry.path.startsWith("/api/admin/")
  )
  expect(writes.map(entry => [entry.method, entry.path])).toEqual([
    ["PUT", `${itemPath}/why`],
    ["PATCH", eventPath],
    ["POST", publishPath],
  ])
  expect(writes[0]?.facts).toEqual({
    body: { why: "降息預期延後，資金回流美元資產。" },
  })
  expect(writes[1]?.facts).toEqual({ body: { headline: "聯準會按兵不動" } })
  await page.screenshot({
    path: testInfo.outputPath("newsroom-review-zh-hant.png"),
    fullPage: true,
  })
})

for (const [locale, title] of [
  ["zh-hans", "重点新闻审核"],
  ["en", "Key news review"],
] as const) {
  test(`${locale}: the review console is translated`, async ({
    page,
    context,
    request,
  }) => {
    await resetMockApi(request)
    await authenticateAs(context, "admin")
    await page.goto(`/${locale}/admin/newsroom`)
    await expect(
      page.getByRole("heading", { name: title, level: 1 })
    ).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole("tab")).toHaveCount(3)
  })
}
