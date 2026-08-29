import { useRouter } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

export function LoadingScreen() {
  const { t } = useTranslation()
  return (
    <main
      className="surface-panel mx-auto mt-[12vh] w-[min(calc(100%-2rem),36rem)] border-t-[3px] border-t-lagoon p-8 text-center"
      aria-live="polite"
      role="status"
    >
      <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-line border-t-lagoon-deep" />
      <p className="m-0 text-sea-ink-soft">{t("loading")}</p>
    </main>
  )
}

export function ForbiddenScreen() {
  const { t } = useTranslation()
  return (
    <main className="surface-panel mx-auto mt-[12vh] w-[min(calc(100%-2rem),36rem)] border-t-[3px] border-t-market-caution p-8 text-center">
      <h1 className="m-0 text-5xl font-extrabold">403</h1>
      <p>{t("forbidden")}</p>
    </main>
  )
}

export function NotFoundScreen() {
  const { t } = useTranslation()
  return (
    <main className="surface-panel mx-auto mt-[12vh] w-[min(calc(100%-2rem),36rem)] border-t-[3px] border-t-market-caution p-8 text-center">
      <h1 className="m-0 text-5xl font-extrabold">404</h1>
      <p>{t("notFound")}</p>
    </main>
  )
}

export function ErrorScreen({ error }: { error: Error }) {
  const router = useRouter()
  const { t } = useTranslation()
  const requestId =
    "requestId" in error && typeof error.requestId === "string"
      ? error.requestId
      : null
  return (
    <main
      className="surface-panel mx-auto mt-[12vh] w-[min(calc(100%-2rem),36rem)] border-t-[3px] border-t-market-up p-8 text-center"
      role="alert"
    >
      <h1 className="mt-0 text-2xl">{t("unexpectedError")}</h1>
      {requestId ? <p>{t("requestId", { id: requestId })}</p> : null}
      <button type="button" onClick={() => void router.invalidate()}>
        {t("retry")}
      </button>
    </main>
  )
}
