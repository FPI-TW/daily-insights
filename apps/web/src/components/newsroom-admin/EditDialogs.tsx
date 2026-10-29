import { X } from "lucide-react"
import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import type {
  NewsroomEventEdit,
  NewsroomRelatedSymbol,
} from "#/lib/newsroom-admin"

const HEADLINE_MAX = 500
const TEXT_MAX = 2_000

function DialogActions({
  pending,
  onCancel,
  submitLabel,
}: {
  pending: boolean
  onCancel: () => void
  submitLabel: string
}) {
  const { t } = useTranslation()
  return (
    <div className="mt-5 flex justify-end gap-3">
      <button type="button" onClick={onCancel}>
        {t("dismiss")}
      </button>
      <button type="submit" className="primary-action" disabled={pending}>
        {submitLabel}
      </button>
    </div>
  )
}

/** Edit the shared zh-hant headline, summary and related symbols of an event. */
export function EventEditDialog({
  open,
  headline,
  summary,
  symbols,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean
  headline: string
  summary: string
  symbols: NewsroomRelatedSymbol[]
  pending: boolean
  error: string
  onClose: () => void
  onSubmit: (changes: NewsroomEventEdit) => void
}) {
  const { t } = useTranslation()
  const titleId = useId()
  const [draftHeadline, setDraftHeadline] = useState(headline)
  const [draftSummary, setDraftSummary] = useState(summary)
  const [draftSymbols, setDraftSymbols] = useState(symbols)
  const [invalid, setInvalid] = useState(false)

  const submit = () => {
    const nextHeadline = draftHeadline.trim()
    const nextSummary = draftSummary.trim()
    if (!nextHeadline || !nextSummary) {
      setInvalid(true)
      return
    }
    const changes: NewsroomEventEdit = {}
    if (nextHeadline !== headline) changes.headline = nextHeadline
    if (nextSummary !== summary) changes.summary = nextSummary
    if (draftSymbols.length !== symbols.length) {
      changes.related_symbols = draftSymbols
    }
    if (Object.keys(changes).length === 0) {
      onClose()
      return
    }
    onSubmit(changes)
  }

  return (
    <Dialog open={open} onClose={onClose} labelledBy={titleId}>
      <form
        className="grid gap-4"
        onSubmit={event => {
          event.preventDefault()
          submit()
        }}
      >
        <h2 id={titleId} className="m-0 text-lg font-extrabold">
          {t("newsroomAdminEditEventTitle")}
        </h2>
        <p className="m-0 text-sm text-sea-ink-soft">
          {t("newsroomAdminEditLanguageNote")}
        </p>
        <label>
          {t("newsroomAdminHeadline")}
          <input
            value={draftHeadline}
            maxLength={HEADLINE_MAX}
            onChange={event => setDraftHeadline(event.target.value)}
          />
        </label>
        <label>
          {t("newsroomAdminSummary")}
          <textarea
            value={draftSummary}
            maxLength={TEXT_MAX}
            onChange={event => setDraftSummary(event.target.value)}
          />
        </label>
        {symbols.length > 0 ? (
          <fieldset className="m-0 grid gap-2 border-0 p-0">
            <legend className="mb-2 text-sm font-bold">
              {t("newsroomAdminRelatedSymbols")}
            </legend>
            <div className="flex flex-wrap gap-2">
              {draftSymbols.map(symbol => (
                <span
                  key={symbol.symbol}
                  className="inline-flex items-center gap-1 rounded-full border border-chip-line bg-chip py-0.5 pr-1 pl-3 text-sm"
                >
                  {symbol.label} · {symbol.symbol}
                  <button
                    type="button"
                    className="grid size-6 place-items-center rounded-full border-0 bg-transparent p-0 text-sea-ink-soft hover:bg-link-hover"
                    aria-label={t("newsroomAdminRemoveSymbol", {
                      symbol: symbol.label,
                    })}
                    onClick={() =>
                      setDraftSymbols(current =>
                        current.filter(item => item.symbol !== symbol.symbol)
                      )
                    }
                  >
                    <X className="size-3.5" aria-hidden="true" />
                  </button>
                </span>
              ))}
            </div>
          </fieldset>
        ) : null}
        {invalid ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {t("newsroomAdminRequiredText")}
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {error}
          </p>
        ) : null}
        <DialogActions
          pending={pending}
          onCancel={onClose}
          submitLabel={t("newsroomAdminSave")}
        />
      </form>
    </Dialog>
  )
}

/** Edit one long zh-hant text: a market's "why it matters" or a pasted body. */
export function TextEditDialog({
  open,
  title,
  description,
  label,
  initial,
  maxLength,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean
  title: string
  description?: string
  label: string
  initial: string
  maxLength: number
  pending: boolean
  error: string
  onClose: () => void
  onSubmit: (value: string) => void
}) {
  const { t } = useTranslation()
  const titleId = useId()
  const [value, setValue] = useState(initial)
  const [invalid, setInvalid] = useState(false)

  return (
    <Dialog open={open} onClose={onClose} labelledBy={titleId}>
      <form
        className="grid gap-4"
        onSubmit={event => {
          event.preventDefault()
          const next = value.trim()
          if (!next) {
            setInvalid(true)
            return
          }
          if (next === initial) {
            onClose()
            return
          }
          onSubmit(next)
        }}
      >
        <h2 id={titleId} className="m-0 text-lg font-extrabold">
          {title}
        </h2>
        {description ? (
          <p className="m-0 text-sm text-sea-ink-soft">{description}</p>
        ) : null}
        <label>
          {label}
          <textarea
            className="min-h-40"
            value={value}
            maxLength={maxLength}
            onChange={event => setValue(event.target.value)}
          />
        </label>
        <p className="m-0 text-right text-xs text-sea-ink-soft">
          {t("newsroomAdminCharacters", { count: value.length, maxLength })}
        </p>
        {invalid ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {t("newsroomAdminRequiredText")}
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {error}
          </p>
        ) : null}
        <DialogActions
          pending={pending}
          onCancel={onClose}
          submitLabel={t("newsroomAdminSave")}
        />
      </form>
    </Dialog>
  )
}

export const WHY_MAX = TEXT_MAX
export const BODY_MAX = 40_000
