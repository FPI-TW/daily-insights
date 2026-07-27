import {
  ApiError,
  type Locale,
  type Member,
  type Organization,
  type ProvisionedMember,
} from "@daily-insights/api-client"
import { useForm } from "@tanstack/react-form"
import { useRouter } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { browserAdministrationClient } from "#/lib/admin-members"
import { requireCsrfToken } from "#/lib/auth"
import { useSessionExpiryRedirect } from "#/lib/useSessionExpiry"

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
    <main className="member-admin-page">
      <header className="member-admin-hero">
        <p className="eyebrow">{t("adminPortal")}</p>
        <h1>{t("memberManagementTitle")}</h1>
        <p>{t("memberManagementDescription")}</p>
      </header>

      {provisionedMember ? (
        <TemporaryPasswordNotice
          member={provisionedMember}
          onDismiss={() => setProvisionedMember(null)}
        />
      ) : null}

      <div className="member-admin-layout">
        <aside className="organization-panel">
          <div className="organization-panel-heading">
            <div>
              <p className="eyebrow">{t("organizations")}</p>
              <h2>{t("organizationListTitle")}</h2>
            </div>
            <span className="organization-count">{directory.length}</span>
          </div>

          <div className="organization-list">
            {directory.map(({ organization }) => (
              <button
                key={organization.id}
                type="button"
                className="organization-option"
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

        <section className="member-workspace">
          {selectedEntry ? (
            <>
              <header className="member-workspace-heading">
                <div>
                  <p className="eyebrow">{selectedEntry.organization.slug}</p>
                  <h2>{selectedEntry.organization.name}</h2>
                </div>
                <div className="seat-meter">
                  <span>
                    {t("seatUsage", {
                      used: selectedEntry.organization.seat_count,
                      total: selectedEntry.organization.seat_limit,
                    })}
                  </span>
                  <progress
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

              <section
                className="member-list-section"
                aria-labelledby="member-list-title"
              >
                <div className="member-list-heading">
                  <h2 id="member-list-title">{t("memberListTitle")}</h2>
                  <span>{selectedEntry.members.length}</span>
                </div>
                {selectedEntry.members.length === 0 ? (
                  <div className="member-empty">
                    <h3>{t("memberEmptyTitle")}</h3>
                    <p>{t("memberEmptyDescription")}</p>
                  </div>
                ) : (
                  <div className="member-list">
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
            <div className="member-empty member-empty--workspace">
              <h2>{t("organizationEmptyTitle")}</h2>
              <p>{t("organizationEmptyDescription")}</p>
            </div>
          )}
        </section>
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
    <details className="organization-create">
      <summary>{t("createOrganization")}</summary>
      <form
        className="member-admin-form"
        onSubmit={event => {
          event.preventDefault()
          void form.handleSubmit()
        }}
      >
        <div className="member-form-grid">
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
          <p className="member-form-error" role="alert">
            {error}
          </p>
        ) : null}
        <form.Subscribe selector={state => state.isSubmitting}>
          {isSubmitting => (
            <button type="submit" disabled={isSubmitting}>
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
      className="member-create-card"
      onSubmit={event => {
        event.preventDefault()
        void form.handleSubmit()
      }}
    >
      <header>
        <div>
          <p className="eyebrow">{t("newMember")}</p>
          <h2>{t("createMemberTitle")}</h2>
        </div>
        {!seatsAvailable ? (
          <span className="seat-limit-warning">{t("seatLimitReached")}</span>
        ) : null}
      </header>
      <div className="member-form-grid">
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
        <p className="member-form-error" role="alert">
          {error}
        </p>
      ) : null}
      <form.Subscribe selector={state => state.isSubmitting}>
        {isSubmitting => (
          <button type="submit" disabled={isSubmitting || !seatsAvailable}>
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
    <section className="temporary-password-notice" aria-live="polite">
      <div>
        <p className="eyebrow">{t("memberCreated")}</p>
        <h2>{t("temporaryPasswordTitle")}</h2>
        <p>{t("temporaryPasswordDescription", { email: member.email })}</p>
      </div>
      <code>{member.temporary_password}</code>
      <div className="temporary-password-actions">
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
    <article className="member-card">
      <header>
        <div className="member-avatar" aria-hidden="true">
          {member.display_name.slice(0, 1).toUpperCase()}
        </div>
        <div className="member-identity">
          <h3>{member.display_name}</h3>
          <p>{member.email}</p>
        </div>
        <span className="member-status" data-status={member.status}>
          {t(`memberStatus_${member.status}`)}
        </span>
      </header>
      <dl className="member-meta">
        <div>
          <dt>{t("joinedAt")}</dt>
          <dd>
            {new Intl.DateTimeFormat(locale, {
              dateStyle: "medium",
            }).format(new Date(member.joined_at))}
          </dd>
        </div>
        <div>
          <dt>{t("passwordState")}</dt>
          <dd>
            {member.must_change_password
              ? t("passwordChangePending")
              : t("passwordReady")}
          </dd>
        </div>
      </dl>
      <details className="member-actions">
        <summary>{t("manageMember")}</summary>
        <form
          className="member-admin-form"
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
            <p className="member-form-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className="member-action-buttons">
            <button type="submit" disabled={pendingAction !== ""}>
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
              className="destructive-button"
              type="button"
              disabled={pendingAction !== ""}
              onClick={() => void removeMember()}
            >
              {pendingAction === "remove" ? t("submitting") : t("removeMember")}
            </button>
          </div>
        </form>
      </details>
    </article>
  )
}
