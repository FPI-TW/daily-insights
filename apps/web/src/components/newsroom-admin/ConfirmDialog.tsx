import { useId } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"

/** Ask before an action that cannot be undone from the console. */
export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  pending,
  error,
  onClose,
  onConfirm,
}: {
  title: string
  message: string
  confirmLabel: string
  pending: boolean
  error: string
  onClose: () => void
  onConfirm: () => void
}) {
  const { t } = useTranslation()
  const titleId = useId()
  return (
    <Dialog open onClose={onClose} labelledBy={titleId} role="alertdialog">
      <h2 id={titleId} className="m-0 text-lg font-extrabold">
        {title}
      </h2>
      <p className="mt-3 text-sm leading-6 text-sea-ink-soft">{message}</p>
      {error ? (
        <p role="alert" className="mt-3 mb-0 font-bold text-destructive">
          {error}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end gap-3">
        <button type="button" onClick={onClose}>
          {t("dismiss")}
        </button>
        <button
          type="button"
          className="primary-action"
          disabled={pending}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
      </div>
    </Dialog>
  )
}
