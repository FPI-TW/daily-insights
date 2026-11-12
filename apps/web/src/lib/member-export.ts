import type { Member, Organization } from "@daily-insights/api-client"

type DirectoryEntry = {
  organization: Organization
  members: Member[]
}

const headers = ["使用者名稱", "帳號狀態", "所屬單位", "電子信箱"]

const statusLabels: Record<Member["status"], string> = {
  active: "啟用",
  suspended: "停用",
  invited: "待啟用",
}

function csvCell(value: string): string {
  const safeValue = /^[\s\u0000-\u001f]*[=+\-@]/.test(value)
    ? `'${value}`
    : value
  return /[",\r\n]/.test(safeValue)
    ? `"${safeValue.replaceAll('"', '""')}"`
    : safeValue
}

export function memberDirectoryCsv(directory: DirectoryEntry[]): string {
  const rows = directory.flatMap(({ organization, members }) =>
    members.map(member => [
      member.display_name,
      statusLabels[member.status],
      organization.name,
      member.email,
    ])
  )
  return (
    [headers, ...rows].map(row => row.map(csvCell).join(",")).join("\r\n") +
    "\r\n"
  )
}
