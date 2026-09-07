import type { ReactNode } from "react"

/** Keep every field visible: use labeled two-column records in narrow panels. */
export function ResponsiveTable({ children }: { children: ReactNode }) {
  return (
    <div className="@container min-w-0 w-full">
      <table
        role="table"
        className="block w-full text-sm leading-5 tabular-nums @lg:table @lg:table-fixed @lg:[&_thead_th:first-child]:w-1/5 [&_thead]:sr-only @lg:[&_thead]:not-sr-only @lg:[&_thead]:table-header-group [&_tbody]:block @lg:[&_tbody]:table-row-group [&_tbody_tr]:grid [&_tbody_tr]:grid-cols-2 [&_tbody_tr]:gap-x-4 [&_tbody_tr]:gap-y-3 [&_tbody_tr]:py-4 @lg:[&_tbody_tr]:table-row @lg:[&_tbody_tr]:py-0 [&_tbody_th]:col-span-2 [&_tbody_td]:min-w-0 [&_tbody_th]:min-w-0 [&_td]:whitespace-normal [&_th]:whitespace-normal [&_td]:wrap-anywhere [&_th]:wrap-anywhere [&_tbody_td]:px-2 [&_tbody_td]:py-1 [&_tbody_th]:px-2 [&_tbody_th]:py-1 @lg:[&_tbody_td]:px-2 @lg:[&_tbody_td]:py-1 @lg:[&_tbody_th]:px-2 @lg:[&_tbody_th]:py-1 [&_thead_th]:px-2 [&_thead_th]:py-1.5 [&_tbody_td]:before:mb-1 [&_tbody_td]:before:block [&_tbody_td]:before:font-sans [&_tbody_td]:before:text-xs [&_tbody_td]:before:font-normal [&_tbody_td]:before:text-sea-ink-soft [&_tbody_td]:before:content-[attr(data-label)] @lg:[&_tbody_td]:before:hidden"
      >
        {children}
      </table>
    </div>
  )
}
