import { useRouter } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"

export function LoadingScreen() {
  const { t } = useTranslation()
  return (
    <main className="shell-card" aria-live="polite">
      <p>{t("loading")}</p>
    </main>
  )
}

export function ForbiddenScreen() {
  const { t } = useTranslation()
  return (
    <main className="shell-card">
      <h1>403</h1>
      <p>{t("forbidden")}</p>
    </main>
  )
}

export function NotFoundScreen() {
  const { t } = useTranslation()
  return (
    <main className="shell-card">
      <h1>404</h1>
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
    <main className="shell-card" role="alert">
      <h1>{t("unexpectedError")}</h1>
      {requestId ? <p>{t("requestId", { id: requestId })}</p> : null}
      <button type="button" onClick={() => void router.invalidate()}>
        {t("retry")}
      </button>
    </main>
  )
}
