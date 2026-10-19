import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, expect, it } from "vitest"
import { createI18n } from "#/lib/i18n"
import { JobKindBadge, StatusBadge } from "./DataManagementBadges"

afterEach(cleanup)
it.each([
  "pending",
  "running",
  "retry_wait",
  "succeeded",
  "no_change",
  "partial",
  "unavailable",
  "failed",
  "cancelled",
  "unexpected",
])("renders %s with localized text and a decorative icon", status => {
  const { container } = render(
    <I18nextProvider i18n={createI18n("en")}>
      <StatusBadge status={status} />
    </I18nextProvider>
  )
  expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true")
  expect(container.textContent).not.toBe(status)
  expect(container.textContent).not.toContain("dataManagementStatus")
  if (status === "unexpected")
    expect(screen.getByText("Unknown status: unexpected")).toBeInTheDocument()
})
it.each(["zh-hant", "zh-hans", "en"] as const)(
  "localizes both job kinds in %s",
  locale => {
    const { container } = render(
      <I18nextProvider i18n={createI18n(locale)}>
        <JobKindBadge kind="function" />
        <JobKindBadge kind="projection" />
        <StatusBadge status="retry_wait" />
      </I18nextProvider>
    )
    expect(container.querySelectorAll("svg")).toHaveLength(3)
    expect(container.textContent).not.toContain("dataManagement")
  }
)

it("identifies the publication-only news job without relabeling mixed refresh jobs", () => {
  render(
    <I18nextProvider i18n={createI18n("en")}>
      <JobKindBadge kind="function" jobKey="news_publish_job" />
      <JobKindBadge kind="function" jobKey="news_us_equity_refresh_job" />
    </I18nextProvider>
  )
  expect(screen.getByText("Publish")).toBeInTheDocument()
  expect(screen.getByText("Refresh")).toBeInTheDocument()
})
