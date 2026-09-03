import {
  browserAuthClient,
  rememberCsrfToken,
  requireCsrfToken,
} from "#/lib/auth"

// Shared by the forced first-login form and the account page dialog. Throws
// the ApiError from the client; callers decide how to present it.
export async function changePassword(
  currentPassword: string,
  newPassword: string
) {
  const result = await browserAuthClient().changePassword(
    {
      current_password: currentPassword,
      new_password: newPassword,
    },
    await requireCsrfToken()
  )
  rememberCsrfToken(result.csrf_token)
  return result
}

export const passwordMinimumLength = 8
