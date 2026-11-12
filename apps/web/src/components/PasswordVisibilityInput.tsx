import { Eye, EyeOff } from "lucide-react"
import { useState, type InputHTMLAttributes } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "#/lib/utils"

export function PasswordVisibilityInput({
  className,
  ...inputProps
}: Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  const { t } = useTranslation()
  const [visible, setVisible] = useState(false)

  return (
    <div className="relative">
      <input
        {...inputProps}
        type={visible ? "text" : "password"}
        className={cn("pr-12", className)}
        autoCapitalize="off"
        spellCheck={false}
      />
      <button
        type="button"
        className="absolute inset-y-0 right-1 my-auto grid size-9 place-items-center border-0 bg-transparent p-0 text-sea-ink-soft hover:bg-link-hover hover:text-sea-ink"
        aria-label={t(visible ? "hidePassword" : "showPassword")}
        aria-pressed={visible}
        onClick={() => setVisible(current => !current)}
      >
        {visible ? (
          <EyeOff className="size-4" aria-hidden="true" />
        ) : (
          <Eye className="size-4" aria-hidden="true" />
        )}
      </button>
    </div>
  )
}
