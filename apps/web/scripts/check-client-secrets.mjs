import { readdirSync, readFileSync } from "node:fs"
import { extname, join, resolve } from "node:path"

const clientDirectory = resolve(".output/public")
const forbiddenMarkers = [
  "API_INTERNAL_URL",
  "DATABASE_URL",
  "FINDB_API_KEY",
  "MODEL_API_KEY",
  "R2_ACCESS_KEY_ID",
  "R2_SECRET_ACCESS_KEY",
]

function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? files(path) : [path]
  })
}

const offenders = files(clientDirectory).filter(path => {
  if (![".js", ".css", ".html", ".json"].includes(extname(path))) return false
  const content = readFileSync(path, "utf8")
  return forbiddenMarkers.some(marker => content.includes(marker))
})

if (offenders.length > 0) {
  throw new Error(
    `Browser bundle contains server-only configuration markers:\n${offenders.join("\n")}`
  )
}
