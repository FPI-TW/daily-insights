import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { I18nextProvider } from "react-i18next"
import { createI18n } from "#/lib/i18n"
import { TokenUsageDetails } from "./TokenUsage"
import type { TokenUsage } from "#/lib/admin-chat"

afterEach(cleanup)
const show = (usage: TokenUsage) =>
  render(
    <I18nextProvider i18n={createI18n("en")}>
      <TokenUsageDetails usage={usage} />
    </I18nextProvider>
  )
describe("reported token usage", () => {
  it("distinguishes missing usage from an actual zero", () => {
    show({
      input_tokens: null,
      output_tokens: 0,
      total_tokens: 0,
      completeness: "partial",
    })
    expect(screen.getByText("Not reported")).toBeInTheDocument()
    expect(screen.getAllByText("0")).toHaveLength(2)
    expect(screen.getByText("Known usage")).toBeInTheDocument()
  })
  it("does not invent a zero total when no usage was reported", () => {
    show({
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      completeness: "unavailable",
    })
    expect(screen.getAllByText("Not reported")).toHaveLength(3)
    expect(screen.queryByText("0")).not.toBeInTheDocument()
  })
})
