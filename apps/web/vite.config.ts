import { resolve } from "node:path"
import { defineConfig, loadEnv } from "vite"
import { devtools } from "@tanstack/devtools-vite"

import { tanstackStart } from "@tanstack/react-start/plugin/vite"

import viteReact from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { nitro } from "nitro/vite"
import { z } from "zod"

const serviceDirectory = resolve(import.meta.dirname)
const apiOriginSchema = z
  .url()
  .refine(value => ["http:", "https:"].includes(new URL(value).protocol))
  .transform(value => new URL(value).origin)

const config = defineConfig(({ command, mode }) => {
  const fileEnvironment = loadEnv(mode, serviceDirectory, "")
  const configuredApiUrl =
    process.env.API_INTERNAL_URL || fileEnvironment.API_INTERNAL_URL
  const parsedApiOrigin = apiOriginSchema.safeParse(configuredApiUrl)

  if (command === "serve" && !configuredApiUrl) {
    throw new Error("API_INTERNAL_URL is required. Set it in apps/web/.env.")
  }
  if (configuredApiUrl && !parsedApiOrigin.success) {
    throw new Error("API_INTERNAL_URL must be an absolute HTTP(S) origin.")
  }
  const apiOrigin = parsedApiOrigin.success ? parsedApiOrigin.data : undefined

  if (command === "serve" && apiOrigin) {
    process.env.API_INTERNAL_URL = apiOrigin
  }

  return {
    envDir: serviceDirectory,
    resolve: { tsconfigPaths: true },
    plugins: [
      devtools(),
      nitro({
        devProxy: apiOrigin
          ? {
              "/api/**": {
                target: apiOrigin,
                changeOrigin: false,
              },
            }
          : {},
        rollupConfig: { external: [/^@sentry\//] },
      }),
      tailwindcss(),
      tanstackStart(),
      viteReact(),
    ],
  }
})

export default config
