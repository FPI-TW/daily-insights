import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import type { Member } from "@daily-insights/api-client"
import { ApiError } from "@daily-insights/api-client"
import { createI18n } from "#/lib/i18n"
import { MemberManagementPage } from "./MemberManagementPage"

const { createMember, updateMember, invalidate } = vi.hoisted(() => ({
  createMember: vi.fn(),
  updateMember: vi.fn(),
  invalidate: vi.fn(),
}))
vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({ createMember, updateMember }),
}))
vi.mock("#/lib/auth", () => ({ requireCsrfToken: async () => "csrf" }))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => async () => false,
}))
vi.mock("@tanstack/react-router", () => ({ useRouter: () => ({ invalidate }) }))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})
const member: Member = {
  membership_id: "membership",
  user_id: "user",
  email: "trial@example.com",
  display_name: "Trial User",
  member_type: "trial",
  trial_expires_at: "2026-01-01T00:00:00Z",
  trial_expired: true,
  status: "suspended",
  joined_at: "2025-12-25T00:00:00Z",
  must_change_password: true,
}
const show = (members: Member[] = [], seats = 0) =>
  render(
    <I18nextProvider i18n={createI18n("en")}>
      <MemberManagementPage
        locale="en"
        directory={[
          {
            organization: {
              id: "org",
              name: "Organization",
              slug: "organization",
              seat_count: seats,
              seat_limit: 2,
              status: "active",
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
            members,
          },
        ]}
      />
    </I18nextProvider>
  )

describe("trial member administration", () => {
  it("creates a trial with default seven days and the same account fields", async () => {
    createMember.mockResolvedValue({
      ...member,
      status: "active",
      trial_expired: false,
      temporary_password: "Aa12!testpass",
    })
    show()
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "New Trial" },
    })
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "new@example.com" },
    })
    fireEvent.change(screen.getByLabelText("Member type"), {
      target: { value: "trial" },
    })
    expect(screen.getByLabelText("Trial days")).toHaveValue(7)
    fireEvent.submit(screen.getByLabelText("Trial days").closest("form")!)
    await waitFor(() =>
      expect(createMember).toHaveBeenCalledWith(
        "org",
        {
          display_name: "New Trial",
          email: "new@example.com",
          member_type: "trial",
          trial_days: 7,
          reason: "new_seat",
        },
        "csrf"
      )
    )
    expect(await screen.findByText("Aa12!testpass")).toBeInTheDocument()
  })
  it("filters members and preserves a suspended status while editing the Taipei deadline", async () => {
    updateMember.mockResolvedValue(member)
    show([
      member,
      {
        ...member,
        user_id: "regular",
        email: "regular@example.com",
        display_name: "Regular User",
        member_type: "regular",
        trial_expires_at: null,
        trial_expired: false,
      },
    ])
    fireEvent.click(screen.getByRole("button", { name: "Trial members" }))
    expect(screen.queryByText("Regular User")).not.toBeInTheDocument()
    expect(screen.getByText("Trial expired")).toBeInTheDocument()
    const input = screen.getByLabelText(/Trial expiry \(Taipei UTC\+8\)/)
    fireEvent.change(input, { target: { value: "2027-01-01T08:30" } })
    fireEvent.submit(input.closest("form")!)
    await waitFor(() =>
      expect(updateMember).toHaveBeenCalledWith(
        "org",
        "user",
        {
          display_name: "Trial User",
          trial_expires_at: "2027-01-01T00:30:00.000Z",
          reason: "access_review",
        },
        "csrf"
      )
    )
  })
  it("explains a capacity conflict when converting an expired trial", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true)
    updateMember.mockRejectedValue(
      new ApiError(409, null, "organization seat limit reached", null)
    )
    show([member], 2)
    fireEvent.click(
      screen.getByRole("button", { name: "Convert to regular", hidden: true })
    )
    await waitFor(() =>
      expect(updateMember).toHaveBeenCalledWith(
        "org",
        "user",
        { member_type: "regular", reason: "access_review" },
        "csrf"
      )
    )
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No seats available"
    )
  })
})
