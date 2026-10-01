import { useForm } from "@tanstack/react-form"
import { useId } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "#/components/Dialog"
import {
  newsroomMarkets,
  newsroomSourceKinds,
  type NewsroomSource,
  sourceFormDefaults,
  sourceFormSchema,
  type SourceFormValues,
} from "#/lib/newsroom-admin"

function FieldErrors({ errors }: { errors: unknown[] }) {
  const { t } = useTranslation()
  const messages = [
    ...new Set(
      errors
        .map(error =>
          typeof error === "string"
            ? error
            : error && typeof error === "object" && "message" in error
              ? String(error.message)
              : ""
        )
        .filter(Boolean)
    ),
  ]
  if (messages.length === 0) return null
  return (
    <span role="alert" className="text-xs font-bold text-destructive">
      {messages.map(message => t(message)).join(" ")}
    </span>
  )
}

/** Create or edit one newsroom source (D3). Validation mirrors the API. */
export function SourceFormDialog({
  source,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  source: NewsroomSource | null
  pending: boolean
  error: string
  onClose: () => void
  onSubmit: (values: SourceFormValues) => Promise<unknown>
}) {
  const { t } = useTranslation()
  const titleId = useId()
  const form = useForm({
    defaultValues: sourceFormDefaults(source),
    validators: { onSubmit: sourceFormSchema },
    onSubmit: async ({ value }) => {
      await onSubmit(value)
    },
  })
  const manual = source?.kind === "manual"

  return (
    <Dialog
      open
      onClose={onClose}
      labelledBy={titleId}
      panelClassName="max-w-2xl"
    >
      <form
        className="grid gap-4"
        noValidate
        onSubmit={event => {
          event.preventDefault()
          void form.handleSubmit()
        }}
      >
        <h2 id={titleId} className="m-0 text-lg font-extrabold">
          {source
            ? t("newsroomAdminSourceEditTitle", { name: source.name })
            : t("newsroomAdminSourceCreateTitle")}
        </h2>
        <div className="grid max-h-[65vh] grid-cols-2 gap-3 overflow-y-auto pr-1 max-[42rem]:grid-cols-1">
          <form.Field name="name">
            {field => (
              <label>
                {t("newsroomAdminSourceName")}
                <input
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="key">
            {field => (
              <label>
                {t("newsroomAdminSourceKey")}
                <input
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="kind">
            {field => (
              <label>
                {t("newsroomAdminSourceKind")}
                <select
                  value={field.state.value}
                  disabled={manual}
                  onBlur={field.handleBlur}
                  onChange={event =>
                    field.handleChange(
                      event.target.value as SourceFormValues["kind"]
                    )
                  }
                >
                  {newsroomSourceKinds.map(kind => (
                    <option key={kind} value={kind}>
                      {kind}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </form.Field>
          <form.Field name="hostname">
            {field => (
              <label>
                {t("newsroomAdminSourceHostname")}
                <input
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="url">
            {field => (
              <label className="col-span-2 max-[42rem]:col-span-1">
                {t("newsroomAdminSourceUrl")}
                <input
                  type="url"
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="markets">
            {field => (
              <fieldset className="col-span-2 m-0 grid gap-2 border-0 p-0 max-[42rem]:col-span-1">
                <legend className="mb-2 text-sm font-bold">
                  {t("newsroomAdminSourceMarkets")}
                </legend>
                <div className="flex flex-wrap gap-4">
                  {newsroomMarkets.map(market => (
                    <label
                      key={market}
                      className="flex items-center gap-2 font-normal"
                    >
                      <input
                        type="checkbox"
                        className="size-4 w-auto"
                        checked={field.state.value.includes(market)}
                        onChange={event =>
                          field.handleChange(
                            event.target.checked
                              ? [...field.state.value, market]
                              : field.state.value.filter(
                                  item => item !== market
                                )
                          )
                        }
                      />
                      {t(`newsroomAdminMarket_${market}`)}
                    </label>
                  ))}
                </div>
              </fieldset>
            )}
          </form.Field>
          <form.Field name="trust_tier">
            {field => (
              <label>
                {t("newsroomAdminSourceTrust")}
                <select
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event =>
                    field.handleChange(Number(event.target.value))
                  }
                >
                  {[3, 2, 1].map(tier => (
                    <option key={tier} value={tier}>
                      {t(`newsroomAdminSourceTrust_${tier}`)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </form.Field>
          <form.Field name="weight">
            {field => (
              <label>
                {t("newsroomAdminSourceWeight")}
                <input
                  type="number"
                  min={0.5}
                  max={2}
                  step={0.1}
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event =>
                    field.handleChange(event.target.valueAsNumber)
                  }
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="poll_interval_minutes">
            {field => (
              <label>
                {t("newsroomAdminSourceInterval")}
                <input
                  type="number"
                  min={5}
                  max={1440}
                  step={5}
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event =>
                    field.handleChange(event.target.valueAsNumber)
                  }
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="language_filter">
            {field => (
              <label>
                {t("newsroomAdminSourceLanguages")}
                <input
                  value={field.state.value}
                  placeholder="en, zh-tw"
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="link_pattern">
            {field => (
              <label className="col-span-2 max-[42rem]:col-span-1">
                {t("newsroomAdminSourcePattern")}
                <input
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
                <FieldErrors errors={field.state.meta.errors} />
              </label>
            )}
          </form.Field>
          <form.Field name="full_text_in_feed">
            {field => (
              <label className="flex items-center gap-2 font-normal">
                <input
                  type="checkbox"
                  className="size-4 w-auto"
                  checked={field.state.value}
                  onChange={event => field.handleChange(event.target.checked)}
                />
                {t("newsroomAdminSourceFullText")}
              </label>
            )}
          </form.Field>
          <form.Field name="enabled">
            {field => (
              <label className="flex items-center gap-2 font-normal">
                <input
                  type="checkbox"
                  className="size-4 w-auto"
                  checked={field.state.value}
                  onChange={event => field.handleChange(event.target.checked)}
                />
                {t("newsroomAdminSourceEnabled")}
              </label>
            )}
          </form.Field>
        </div>
        {error ? (
          <p role="alert" className="m-0 font-bold text-destructive">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-3">
          <button type="button" onClick={onClose}>
            {t("dismiss")}
          </button>
          <form.Subscribe selector={state => state.isSubmitting}>
            {submitting => (
              <button
                type="submit"
                className="primary-action"
                disabled={pending || submitting}
              >
                {t("newsroomAdminSave")}
              </button>
            )}
          </form.Subscribe>
        </div>
      </form>
    </Dialog>
  )
}
