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
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("accountEyebrow")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("accountTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">{t("accountDescription")}</p>
      </header>
      <section
        className="surface-panel grid overflow-hidden border-t-[3px] border-t-lagoon sm:grid-cols-[minmax(0,1fr)_minmax(13rem,0.42fr)]"
        aria-labelledby="profile-title"
      >
        <div className="flex items-center gap-4 p-[clamp(1.25rem,3vw,1.75rem)]">
          <div
            className="grid h-12 w-12 shrink-0 place-items-center rounded-[10px] bg-lagoon text-lg font-extrabold text-white"
            aria-hidden="true"
          >
            {user.display_name.slice(0, 1).toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="eyebrow">{t("accountEyebrow")}</p>
            <h2 className="mt-1 mb-0 truncate text-lg" id="profile-title">
              {user.display_name}
            </h2>
            <p className="mt-1 mb-0 truncate font-mono text-sm text-sea-ink-soft">
              {user.email}
            </p>
          </div>
        </div>
        <dl className="grid content-center gap-1 border-t border-line bg-link-hover px-[clamp(1.25rem,3vw,1.75rem)] py-4 sm:border-t-0 sm:border-l">
          <dt className="text-[11px] font-extrabold tracking-[0.12em] text-kicker uppercase">
            {t("accountEyebrow")}
          </dt>
          <dd className="m-0 font-mono text-sm font-bold text-sea-ink">
            {t(`role_${user.system_role}`)}
          </dd>
        </dl>
      </section>
    </main>
  )
}
