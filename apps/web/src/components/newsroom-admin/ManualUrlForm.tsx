import type { Locale } from "@daily-insights/api-client"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { newsroomAdminKeys } from "#/lib/newsroom-admin"
import { useNewsroomAction } from "./useNewsroomAction"

const HTTP_URL = /^https?:\/\/\S+$/i

/** Queue an article from outside the source pool into this edition date. */
export function ManualUrlForm({
  locale,
  date,
}: {
  locale: Locale
  date: string
}) {
  const { t } = useTranslation()
  const [url, setUrl] = useState("")
  const [invalid, setInvalid] = useState(false)
  const [submitted, setSubmitted] = useState("")
  const submit = useNewsroomAction({
    locale,
    run: (client, value: string, csrf) =>
      client.submitManualUrl(value, date, csrf),
    // The article enters the untriaged count until triage places it.
    invalidates: () => [newsroomAdminKeys.day(date)],
    onSuccess: (_, value) => {
      setSubmitted(value)
      setUrl("")
    },
  })
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      noValidate
      onSubmit={event => {
        event.preventDefault()
        const value = url.trim()
        setSubmitted("")
        if (!HTTP_URL.test(value)) {
          setInvalid(true)
          return
        }
        setInvalid(false)
        void submit.run(value)
      }}
    >
      <label className="min-w-64 flex-1">
        {t("newsroomAdminManualUrlLabel")}
        <input
          type="url"
          inputMode="url"
          placeholder="https://"
          value={url}
          aria-invalid={invalid}
          onChange={event => setUrl(event.target.value)}
        />
      </label>
      <button
        type="submit"
        className="primary-action"
        disabled={submit.isPending}
      >
        {t("newsroomAdminManualUrlSubmit")}
      </button>
      {invalid ? (
        <p role="alert" className="m-0 w-full font-bold text-destructive">
          {t("newsroomAdminManualUrlInvalid")}
        </p>
      ) : null}
      {submit.error ? (
        <p role="alert" className="m-0 w-full font-bold text-destructive">
          {submit.error}
        </p>
      ) : null}
      {submitted ? (
        <p role="status" className="m-0 w-full text-sm text-palm">
          {t("newsroomAdminManualUrlQueued", { url: submitted })}
        </p>
      ) : null}
    </form>
  )
}
