import { execFileSync } from "node:child_process"
import { mkdtempSync, readFileSync, renameSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const workspaceDirectory = resolve(packageDirectory, "../..")
const checkOnly = process.argv.includes("--check")
const temporaryDirectory = mkdtempSync(
  join(tmpdir(), "daily-insights-openapi-")
)
const outputDirectory = checkOnly ? temporaryDirectory : packageDirectory
const openapiPath = join(outputDirectory, "openapi.json")
const generatedPath = join(outputDirectory, "generated.ts")

try {
  execFileSync(
    "uv",
    [
      "run",
      "--project",
      join(workspaceDirectory, "apps/api"),
      "python",
      join(packageDirectory, "scripts/export_openapi.py"),
      openapiPath,
    ],
    {
      cwd: workspaceDirectory,
      stdio: "inherit",
      env: {
        ...process.env,
        UV_CACHE_DIR:
          process.env.UV_CACHE_DIR ?? join(tmpdir(), "daily-insights-uv-cache"),
      },
    }
  )
  execFileSync(
    "pnpm",
    [
      "--dir",
      packageDirectory,
      "exec",
      "openapi-typescript",
      openapiPath,
      "--output",
      generatedPath,
    ],
    { cwd: workspaceDirectory, stdio: "inherit" }
  )
  execFileSync(
    "pnpm",
    [
      "--dir",
      join(workspaceDirectory, "apps/web"),
      "exec",
      "prettier",
      "--write",
      "--config",
      join(workspaceDirectory, ".prettierrc.json"),
      openapiPath,
      generatedPath,
    ],
    { cwd: workspaceDirectory, stdio: "inherit" }
  )

  if (checkOnly) {
    for (const [actual, expected] of [
      [openapiPath, join(packageDirectory, "openapi.json")],
      [generatedPath, join(packageDirectory, "src/generated.ts")],
    ]) {
      if (readFileSync(actual, "utf8") !== readFileSync(expected, "utf8")) {
        throw new Error(
          `API contract drift detected in ${expected}. Run pnpm generate:api-client.`
        )
      }
    }
  } else {
    renameSync(generatedPath, join(packageDirectory, "src/generated.ts"))
  }
} finally {
  rmSync(temporaryDirectory, { recursive: true, force: true })
}
