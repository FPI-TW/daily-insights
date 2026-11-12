import { describe, expect, it } from "vitest"
import type { Member, Organization } from "@daily-insights/api-client"
import { memberDirectoryCsv } from "./member-export"

const organization = { name: '國泰, "證券"' } as Organization
const member = {
  display_name: "=王\n大明",
  status: "active",
  email: "wang@example.com",
} as Member

describe("memberDirectoryCsv", () => {
  it("exports every organization in the reference column order and escapes CSV cells", () => {
    expect(
      memberDirectoryCsv([
        { organization, members: [member] },
        {
          organization: { ...organization, name: "中華電信" },
          members: [
            {
              ...member,
              display_name: "陳小美",
              status: "suspended",
              email: "chen@example.com",
            },
            {
              ...member,
              display_name: "待邀請",
              status: "invited",
              email: "invite@example.com",
            },
          ],
        },
      ])
    ).toBe(
      '使用者名稱,帳號狀態,所屬單位,電子信箱\r\n"\'=王\n大明",啟用,"國泰, ""證券""",wang@example.com\r\n陳小美,停用,中華電信,chen@example.com\r\n待邀請,待啟用,中華電信,invite@example.com\r\n'
    )
  })

  it("exports a header when the directory is empty", () => {
    expect(memberDirectoryCsv([])).toBe(
      "使用者名稱,帳號狀態,所屬單位,電子信箱\r\n"
    )
  })
})
