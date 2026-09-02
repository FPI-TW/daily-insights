import {
  ApiError,
  type Locale,
  type Member,
  type Organization,
  type ProvisionedMember,
} from "@daily-insights/api-client"
import { useForm } from "@tanstack/react-form"
import { useRouter } from "@tanstack/react-router"
import { motion } from "motion/react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"
import { hoverLift, reveal, springs, useEnterAnimation } from "#/lib/motion"

type DirectoryEntry = {
  organization: Organization
  members: Member[]
}

const organizationReasonOptions = [
  "new_contract",
  "organization_expansion",
  "account_migration",
  "internal_test",
] as const

const memberReasonOptions = [
  "new_seat",
  "staff_change",
  "access_review",
  "support_request",
] as const

export function MemberManagementPage({
  directory,
  locale,
}: {
  directory: DirectoryEntry[]
  locale: Locale
}) {
  const { t } = useTranslation()
  const animate = useEnterAnimation()
  const [selectedOrganizationId, setSelectedOrganizationId] = useState(
    directory[0]?.organization.id ?? ""
  )
  const [provisionedMember, setProvisionedMember] =
    useState<ProvisionedMember | null>(null)

  useEffect(() => {
    const firstOrganization = directory[0]?.organization
    if (
      firstOrganization &&
      !directory.some(entry => entry.organization.id === selectedOrganizationId)
    ) {
      setSelectedOrganizationId(firstOrganization.id)
    }
  }, [directory, selectedOrganizationId])

  const selectedEntry =
    directory.find(entry => entry.organization.id === selectedOrganizationId) ??
    directory[0]

  return (
    <main className="page-shell">
      <header className="mb-8 max-w-3xl">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1 className="mt-2 mb-3 text-[clamp(1.9rem,4vw,2.5rem)] leading-none font-extrabold tracking-[-0.045em]">
          {t("memberManagementTitle")}
        </h1>
        <span
          className="mb-4 block h-[3px] w-14 bg-lagoon"
          aria-hidden="true"
        />
        <p className="leading-7 text-sea-ink-soft">
          {t("memberManagementDescription")}
        </p>
      </header>

      {provisionedMember ? (
        <TemporaryPasswordNotice
          member={provisionedMember}
          onDismiss={() => setProvisionedMember(null)}
        />
      ) : null}

      <div className="grid grid-cols-[minmax(16rem,0.34fr)_minmax(0,1fr)] items-start gap-5 max-[52rem]:grid-cols-1">
        <aside className="surface-panel sticky top-24 grid gap-4 border-t-[3px] border-t-lagoon p-4 max-[52rem]:static">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="eyebrow">{t("organizations")}</p>
              <h2>{t("organizationListTitle")}</h2>
            </div>
            <span className="grid h-7 min-w-7 place-items-center rounded-full bg-link-hover px-2 text-xs font-extrabold text-sea-ink-soft">
              {directory.length}
            </span>
          </div>

          <div className="grid gap-2">
            {directory.map(({ organization }) => (
              <button
                key={organization.id}
                type="button"
                className="grid w-full gap-1 rounded-lg border border-transparent bg-transparent px-3 py-2.5 text-left hover:border-line hover:bg-link-hover aria-pressed:border-lagoon-deep aria-pressed:bg-lagoon-deep aria-pressed:text-white [&>small]:text-xs [&>small]:opacity-75 [&>span]:font-extrabold"
                aria-pressed={
                  organization.id === selectedEntry?.organization.id
                }
                onClick={() => {
                  setSelectedOrganizationId(organization.id)
                  setProvisionedMember(null)
                }}
              >
                <span>{organization.name}</span>
                <small>
                  {t("seatUsage", {
                    used: organization.seat_count,
                    total: organization.seat_limit,
                  })}
                </small>
              </button>
            ))}
          </div>

          <CreateOrganizationForm
            locale={locale}
            onCreated={organization => {
              setSelectedOrganizationId(organization.id)
              setProvisionedMember(null)
            }}
          />
        </aside>

        <motion.section
          className="surface-panel min-w-0 p-[clamp(1rem,3vw,1.5rem)]"
          key={selectedEntry?.organization.id ?? "none"}
          {...reveal(animate)}
        >
          {selectedEntry ? (
            <>
              <header className="flex items-end justify-between gap-4 border-b border-line pb-5 max-[42rem]:items-stretch max-[42rem]:flex-col">
                <div>
                  <p className="eyebrow">{selectedEntry.organization.slug}</p>
                  <h2>{selectedEntry.organization.name}</h2>
                </div>
                <div className="grid min-w-44 gap-2 text-right max-[42rem]:text-left">
                  <span className="text-xs font-extrabold text-sea-ink-soft">
                    {t("seatUsage", {
                      used: selectedEntry.organization.seat_count,
                      total: selectedEntry.organization.seat_limit,
                    })}
                  </span>
                  <progress
                    className="h-2 w-full overflow-hidden rounded-full accent-lagoon-deep"
                    value={selectedEntry.organization.seat_count}
                    max={selectedEntry.organization.seat_limit}
                  />
                </div>
              </header>

              <CreateMemberForm
                organizationId={selectedEntry.organization.id}
                locale={locale}
                seatsAvailable={
                  selectedEntry.organization.seat_count <
                  selectedEntry.organization.seat_limit
                }
                onCreated={setProvisionedMember}
              />

              <section className="mt-7" aria-labelledby="member-list-title">
                <div className="mb-3 flex items-center justify-between gap-4">
                  <h2 id="member-list-title">{t("memberListTitle")}</h2>
                  <span>{selectedEntry.members.length}</span>
                </div>
                {selectedEntry.members.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-line bg-surface p-8 text-center">
                    <h3 className="mt-0">{t("memberEmptyTitle")}</h3>
                    <p className="mb-0 text-sea-ink-soft">
                      {t("memberEmptyDescription")}
                    </p>
                  </div>
                ) : (
                  <div className="grid gap-3">
                    {selectedEntry.members.map(member => (
                      <MemberCard
                        key={member.user_id}
                        member={member}
                        organizationId={selectedEntry.organization.id}
                        locale={locale}
                      />
                    ))}
                  </div>
                )}
              </section>
            </>
          ) : (
            <div className="grid min-h-80 place-content-center rounded-xl border border-dashed border-line bg-surface p-8 text-center">
              <h2 className="mt-0">{t("organizationEmptyTitle")}</h2>
              <p className="mb-0 text-sea-ink-soft">
                {t("organizationEmptyDescription")}
              </p>
            </div>
          )}
        </motion.section>
      </div>
    </main>
  )
}

function CreateOrganizationForm({
  locale,
  onCreated,
}: {
  locale: Locale
  onCreated: (organization: Organization) => void
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [error, setError] = useState("")
  const form = useForm({
    defaultValues: {
      name: "",
      slug: "",
      seatLimit: 5,
      contractReference: "LOCAL",
      reason: "new_contract",
    },
    onSubmit: async ({ value }) => {
      setError("")
      try {
        const organization =
          await browserAdministrationClient().createOrganization(
            {
              name: value.name,
              slug: value.slug,
              seat_limit: value.seatLimit,
              contract_reference: value.contractReference,
              reason: value.reason,
            },
            await requireCsrfToken()
          )
        form.reset()
        await router.invalidate({ sync: true })
        onCreated(organization)
      } catch (caught) {
        if (await redirectExpiredSession(caught)) return
        setError(
          caught instanceof ApiError && caught.status === 409
            ? t("organizationSlugConflict")
            : t("unexpectedError")
        )
      }
    },
  })

  return (
    <details className="border-t border-line pt-4">
      <summary className="cursor-pointer text-sm font-extrabold text-lagoon-deep">
        {t("createOrganization")}
      </summary>
      <form
        className="mt-4 grid gap-3"
        onSubmit={event => {
          event.preventDefault()
          void form.handleSubmit()
        }}
      >
        <div className="grid grid-cols-2 gap-3 max-[42rem]:grid-cols-1">
          <form.Field name="name">
            {field => (
              <label>
                {t("organizationName")}
                <input
                  required
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
              </label>
            )}
          </form.Field>
          <form.Field name="slug">
            {field => (
              <label>
                {t("organizationSlug")}
                <input
                  required
                  pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                  placeholder="example-company"
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
              </label>
            )}
          </form.Field>
          <form.Field name="seatLimit">
            {field => (
              <label>
                {t("seatLimit")}
                <input
                  required
                  min="1"
                  type="number"
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event =>
                    field.handleChange(Number(event.target.value))
                  }
                />
              </label>
            )}
          </form.Field>
          <form.Field name="contractReference">
            {field => (
              <label>
                {t("contractReference")}
                <input
                  required
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
              </label>
            )}
          </form.Field>
        </div>
        <form.Field name="reason">
          {field => (
            <label>
              {t("auditReason")}
              <select
                value={field.state.value}
                onBlur={field.handleBlur}
                onChange={event => field.handleChange(event.target.value)}
              >
                {organizationReasonOptions.map(reason => (
                  <option key={reason} value={reason}>
                    {t(`organizationReason_${reason}`)}
                  </option>
                ))}
              </select>
            </label>
          )}
        </form.Field>
        {error ? (
          <p className="m-0 text-sm font-bold text-red-700" role="alert">
            {error}
          </p>
        ) : null}
        <form.Subscribe selector={state => state.isSubmitting}>
          {isSubmitting => (
            <button
              className="primary-action w-fit"
              type="submit"
              disabled={isSubmitting}
            >
              {isSubmitting ? t("submitting") : t("createOrganization")}
            </button>
          )}
        </form.Subscribe>
      </form>
    </details>
  )
}

function CreateMemberForm({
  organizationId,
  locale,
  seatsAvailable,
  onCreated,
}: {
  organizationId: string
  locale: Locale
  seatsAvailable: boolean
  onCreated: (member: ProvisionedMember) => void
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [error, setError] = useState("")
  const form = useForm({
    defaultValues: {
      displayName: "",
      email: "",
      reason: "new_seat",
    },
    onSubmit: async ({ value }) => {
      setError("")
      try {
        const member = await browserAdministrationClient().createMember(
          organizationId,
          {
            display_name: value.displayName,
            email: value.email,
            reason: value.reason,
          },
          await requireCsrfToken()
        )
        onCreated(member)
        form.reset()
        await router.invalidate({ sync: true })
      } catch (caught) {
        if (await redirectExpiredSession(caught)) return
        setError(
          caught instanceof ApiError && caught.status === 409
            ? t("memberCreateConflict")
            : t("unexpectedError")
        )
      }
    },
  })

  return (
    <form
      className="mt-6 grid gap-4 rounded-[13px] border border-line bg-link-hover p-[clamp(1rem,3vw,1.5rem)]"
      onSubmit={event => {
        event.preventDefault()
        void form.handleSubmit()
      }}
    >
      <header className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">{t("newMember")}</p>
          <h2>{t("createMemberTitle")}</h2>
        </div>
        {!seatsAvailable ? (
          <span className="rounded-full bg-market-caution/15 px-2.5 py-1 text-xs font-extrabold text-market-caution">
            {t("seatLimitReached")}
          </span>
        ) : null}
      </header>
      <div className="grid grid-cols-2 gap-3 max-[42rem]:grid-cols-1">
        <form.Field name="displayName">
          {field => (
            <label>
              {t("displayName")}
              <input
                required
                value={field.state.value}
                onBlur={field.handleBlur}
                onChange={event => field.handleChange(event.target.value)}
              />
            </label>
          )}
        </form.Field>
        <form.Field name="email">
          {field => (
            <label>
              {t("email")}
              <input
                required
                type="email"
                value={field.state.value}
                onBlur={field.handleBlur}
                onChange={event => field.handleChange(event.target.value)}
              />
            </label>
          )}
        </form.Field>
      </div>
      <form.Field name="reason">
        {field => (
          <label>
            {t("auditReason")}
            <select
              value={field.state.value}
              onBlur={field.handleBlur}
              onChange={event => field.handleChange(event.target.value)}
            >
              {memberReasonOptions.map(reason => (
                <option key={reason} value={reason}>
                  {t(`memberReason_${reason}`)}
                </option>
              ))}
            </select>
          </label>
        )}
      </form.Field>
      {error ? (
        <p className="m-0 text-sm font-bold text-red-700" role="alert">
          {error}
        </p>
      ) : null}
      <form.Subscribe selector={state => state.isSubmitting}>
        {isSubmitting => (
          <button
            className="primary-action w-fit"
            type="submit"
            disabled={isSubmitting || !seatsAvailable}
          >
            {isSubmitting ? t("submitting") : t("createMember")}
          </button>
        )}
      </form.Subscribe>
    </form>
  )
}

function TemporaryPasswordNotice({
  member,
  onDismiss,
}: {
  member: ProvisionedMember
  onDismiss: () => void
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  return (
    <section
      className="surface-panel mb-5 grid grid-cols-[minmax(0,1fr)_minmax(14rem,0.55fr)] items-center gap-4 border-l-4 border-l-lagoon p-5 max-[42rem]:grid-cols-1"
      aria-live="polite"
    >
      <div>
        <p className="eyebrow">{t("memberCreated")}</p>
        <h2>{t("temporaryPasswordTitle")}</h2>
        <p>{t("temporaryPasswordDescription", { email: member.email })}</p>
      </div>
      <code className="row-span-2 select-all rounded-lg border border-line bg-surface px-4 py-3 font-mono text-sm text-sea-ink [overflow-wrap:anywhere] max-[42rem]:row-auto">
        {member.temporary_password}
      </code>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard
              .writeText(member.temporary_password)
              .then(() => setCopied(true))
          }}
        >
          {copied ? t("copied") : t("copyPassword")}
        </button>
        <button type="button" onClick={onDismiss}>
          {t("dismiss")}
        </button>
      </div>
    </section>
  )
}

function MemberCard({
  member,
  organizationId,
  locale,
}: {
  member: Member
  organizationId: string
  locale: Locale
}) {
  const { t } = useTranslation()
  const router = useRouter()
  const redirectExpiredSession = useSessionExpiryRedirect(locale, "admin")
  const [error, setError] = useState("")
  const [pendingAction, setPendingAction] = useState("")
  const form = useForm({
    defaultValues: {
      displayName: member.display_name,
      reason: "access_review",
    },
    onSubmit: async ({ value }) => {
      setError("")
      setPendingAction("save")
      try {
        await browserAdministrationClient().updateMember(
          organizationId,
          member.user_id,
          {
            display_name: value.displayName,
            reason: value.reason,
          },
          await requireCsrfToken()
        )
        form.setFieldValue("reason", "access_review")
        await router.invalidate({ sync: true })
      } catch (caught) {
        if (await redirectExpiredSession(caught)) return
        setError(t("unexpectedError"))
      } finally {
        setPendingAction("")
      }
    },
  })

  async function changeStatus() {
    const reason = form.getFieldValue("reason")
    setError("")
    setPendingAction("status")
    try {
      await browserAdministrationClient().updateMember(
        organizationId,
        member.user_id,
        {
          status: member.status === "active" ? "suspended" : "active",
          reason,
        },
        await requireCsrfToken()
      )
      form.setFieldValue("reason", "access_review")
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(t("unexpectedError"))
    } finally {
      setPendingAction("")
    }
  }

  async function removeMember() {
    const reason = form.getFieldValue("reason")
    if (
      !window.confirm(t("removeMemberConfirmation", { email: member.email }))
    ) {
      return
    }
    setError("")
    setPendingAction("remove")
    try {
      await browserAdministrationClient().removeMember(
        organizationId,
        member.user_id,
        reason,
        await requireCsrfToken()
      )
      await router.invalidate({ sync: true })
    } catch (caught) {
      if (await redirectExpiredSession(caught)) return
      setError(t("unexpectedError"))
    } finally {
      setPendingAction("")
    }
  }

  return (
    <motion.article
      className="rounded-[13px] border border-line border-l-[3px] border-l-transparent bg-surface p-4 transition-[border-color,box-shadow] hover:border-l-lagoon hover:shadow-[0_16px_34px_rgb(14_20_19/9%)]"
      whileHover={hoverLift}
      transition={springs.snappy}
    >
      <header className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3">
        <div
          className="grid h-10 w-10 place-items-center rounded-full bg-[linear-gradient(145deg,var(--palm),var(--lagoon-deep))] font-extrabold text-white"
          aria-hidden="true"
        >
          {member.display_name.slice(0, 1).toUpperCase()}
        </div>
        <div className="min-w-0">
          <h3 className="m-0 truncate text-base">{member.display_name}</h3>
          <p className="mt-1 mb-0 truncate text-sm text-sea-ink-soft">
            {member.email}
          </p>
        </div>
        <span
          className={
            member.status === "active"
              ? "rounded-full bg-market-down/15 px-2.5 py-1 text-xs font-extrabold text-market-down"
              : "rounded-full bg-market-caution/15 px-2.5 py-1 text-xs font-extrabold text-market-caution"
          }
        >
          {t(`memberStatus_${member.status}`)}
        </span>
      </header>
      <dl className="my-4 grid grid-cols-2 gap-3 max-[42rem]:grid-cols-1">
        <div className="rounded-lg bg-link-hover p-3">
          <dt className="text-xs font-extrabold text-sea-ink-soft">
            {t("joinedAt")}
          </dt>
          <dd className="mt-1 ml-0 font-mono text-sm font-bold">
            {new Intl.DateTimeFormat(locale, {
              dateStyle: "medium",
            }).format(new Date(member.joined_at))}
          </dd>
        </div>
        <div className="rounded-lg bg-link-hover p-3">
          <dt className="text-xs font-extrabold text-sea-ink-soft">
            {t("passwordState")}
          </dt>
          <dd className="mt-1 ml-0 text-sm font-bold">
            {member.must_change_password
              ? t("passwordChangePending")
              : t("passwordReady")}
          </dd>
        </div>
      </dl>
      <details className="border-t border-line pt-3">
        <summary className="cursor-pointer text-sm font-extrabold text-lagoon-deep">
          {t("manageMember")}
        </summary>
        <form
          className="mt-4 grid gap-3"
          onSubmit={event => {
            event.preventDefault()
            void form.handleSubmit()
          }}
        >
          <form.Field name="displayName">
            {field => (
              <label>
                {t("displayName")}
                <input
                  required
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                />
              </label>
            )}
          </form.Field>
          <form.Field name="reason">
            {field => (
              <label>
                {t("auditReason")}
                <select
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={event => field.handleChange(event.target.value)}
                >
                  {memberReasonOptions.map(reason => (
                    <option key={reason} value={reason}>
                      {t(`memberReason_${reason}`)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </form.Field>
          {error ? (
            <p className="m-0 text-sm font-bold text-red-700" role="alert">
              {error}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <button
              className="primary-action"
              type="submit"
              disabled={pendingAction !== ""}
            >
              {pendingAction === "save" ? t("submitting") : t("saveMember")}
            </button>
            <button
              type="button"
              disabled={pendingAction !== ""}
              onClick={() => void changeStatus()}
            >
              {pendingAction === "status"
                ? t("submitting")
                : member.status === "active"
                  ? t("suspendMember")
                  : t("activateMember")}
            </button>
            <button
              className="border-market-up/30 bg-market-up/10 font-bold text-market-up hover:bg-market-up/15"
              type="button"
              disabled={pendingAction !== ""}
              onClick={() => void removeMember()}
            >
              {pendingAction === "remove" ? t("submitting") : t("removeMember")}
            </button>
          </div>
        </form>
      </details>
    </motion.article>
  )
}
