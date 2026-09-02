import type { Locale } from "@daily-insights/api-client"
import { X } from "lucide-react"
import { useRef, useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { changePassword, passwordMinimumLength } from "#/lib/change-password"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { Dialog } from "./Dialog"

export function ChangePasswordDialog({
  open,
  locale,
  onClose,
  onSuccess,
}: {
  open: boolean
  locale: Locale
  onClose: () => void
  onSuccess: () => void
}) {
  const { t } = useTranslation()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "customer")
  const currentPasswordRef = useRef<HTMLInputElement>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tooShort, setTooShort] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const currentPassword = String(data.get("currentPassword") ?? "")
    const newPassword = String(data.get("newPassword") ?? "")
    if (newPassword.length < passwordMinimumLength) {
      setTooShort(true)
      setError(null)
      return
    }
    setTooShort(false)
    setPending(true)
    setError(null)
    try {
      await changePassword(currentPassword, newPassword)
      form.reset()
      onSuccess()
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(t("accountPasswordError"))
    } finally {
      setPending(false)
    }
  }

  const inputClass =
    "min-h-[42px] w-full rounded-lg border bg-surface px-3 py-2.5 text-sm text-sea-ink placeholder:text-sea-ink-soft/70 hover:border-chip-line/80"

  return (
    <Dialog
      open={open}
      onClose={onClose}
      labelledBy="change-password-title"
      initialFocusRef={currentPasswordRef}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow tracking-[0.08em]">
            {t("accountSecurityEyebrow")}
          </p>
          <h2
            className="mt-1 mb-0 text-lg font-extrabold tracking-[-0.02em] text-sea-ink"
            id="change-password-title"
          >
            {t("changePassword")}
          </h2>
        </div>
        <button
          className="grid min-h-9 min-w-9 place-items-center rounded-md border-0 bg-transparent p-0 text-sea-ink-soft transition-colors hover:bg-link-hover hover:text-sea-ink"
          type="button"
          onClick={onClose}
          aria-label={t("dismiss")}
        >
          <X className="size-4" aria-hidden="true" />
        </button>
      </div>
      <p className="mt-2.5 mb-0 text-xs leading-[1.6] text-sea-ink-soft">
        {t("accountSecurityDescription")}
      </p>
      <form
        className="mt-[18px] grid gap-3.5"
        onSubmit={event => void submit(event)}
      >
        <label className="grid gap-[7px] text-[13px] font-bold">
          {t("currentPassword")}
          <input
            className={`${inputClass} border-chip-line`}
            name="currentPassword"
            type="password"
            autoComplete="current-password"
            placeholder="••••••••"
            ref={currentPasswordRef}
            required
          />
        </label>
        <label className="grid gap-[7px] text-[13px] font-bold">
          {t("newPassword")}
          <input
            className={`${inputClass} ${tooShort ? "border-market-up" : "border-chip-line"}`}
            name="newPassword"
            type="password"
            autoComplete="new-password"
            placeholder={t("passwordMinimumShort")}
            aria-invalid={tooShort || undefined}
            aria-describedby="new-password-hint"
            onChange={() => setTooShort(false)}
            required
          />
        </label>
        <p
          className={`m-0 flex items-center gap-1.5 text-xs leading-[1.6] ${tooShort ? "text-market-up" : "text-sea-ink-soft"}`}
          id="new-password-hint"
        >
          <span
            className="inline-flex size-3.5 shrink-0 items-center justify-center rounded-full bg-secondary text-[9px] font-extrabold text-palm"
            aria-hidden="true"
          >
            i
          </span>
          {t("passwordMinimum")}
        </p>
        {error ? (
          <p
            className="m-0 rounded-lg border border-market-up/35 bg-market-up/10 px-3 py-2 text-sm font-bold text-market-up"
            role="alert"
          >
            {error}
          </p>
        ) : null}
        <div className="mt-1.5 flex justify-end gap-2.5 border-t border-line pt-4">
          <button
            className="min-h-[38px] rounded-lg border border-chip-line bg-surface px-4 py-2 text-[12.5px] font-extrabold text-sea-ink hover:bg-link-hover"
            type="button"
            onClick={onClose}
          >
            {t("cancel")}
          </button>
          <button
            className="primary-action min-h-[42px] rounded-lg px-5 py-2.5 text-[13.5px]"
            type="submit"
            disabled={pending}
          >
            {pending ? t("submitting") : t("changePassword")}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
