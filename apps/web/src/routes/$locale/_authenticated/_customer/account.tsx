import { createFileRoute } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/account"
)({
  component: AccountPage,
})

function AccountPage() {
  const { user } = Route.useRouteContext()
  const { t } = useTranslation()

  return (
    <main className="page-shell">
      <header className="mb-[clamp(2rem,5vw,3.5rem)] max-w-3xl">
        <p className="eyebrow">{t("accountEyebrow")}</p>
        <h1 className="my-3 text-[clamp(2.2rem,6vw,3.8rem)] leading-none font-extrabold tracking-[-0.055em]">
          {t("accountTitle")}
        </h1>
        <p className="leading-7 text-sea-ink-soft">{t("accountDescription")}</p>
      </header>
      <section
        className="surface-panel flex items-center gap-4 p-[clamp(1.25rem,3vw,1.75rem)]"
        aria-labelledby="profile-title"
      >
        <div
          className="grid h-12 w-12 shrink-0 place-items-center rounded-full bg-[linear-gradient(145deg,var(--palm),var(--lagoon-deep))] text-lg font-extrabold text-white"
          aria-hidden="true"
        >
          {user.display_name.slice(0, 1).toUpperCase()}
        </div>
        <div>
          <h2 className="m-0 text-lg" id="profile-title">
            {user.display_name}
          </h2>
          <p className="mt-1 mb-0 text-sm text-sea-ink-soft">{user.email}</p>
        </div>
      </section>
    </main>
  )
}
