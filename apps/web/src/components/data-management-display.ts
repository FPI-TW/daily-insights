import type { JobRun, RoutineRun } from "@daily-insights/api-client"

export function recentRoutines(items: RoutineRun[], taipeiDate: string) {
  const cutoff = new Date(`${taipeiDate}T00:00:00Z`)
  cutoff.setUTCDate(cutoff.getUTCDate() - 4)
  const firstDate = cutoff.toISOString().slice(0, 10)
  return items
    .filter(
      item => item.edition_date >= firstDate && item.edition_date <= taipeiDate
    )
    .sort(
      (a, b) =>
        b.edition_date.localeCompare(a.edition_date) ||
        b.created_at.localeCompare(a.created_at) ||
        a.id.localeCompare(b.id)
    )
}

const timestamp = (run: JobRun) => Date.parse(run.started_at ?? run.created_at)
const compareRuns = (a: JobRun, b: JobRun) =>
  timestamp(a) - timestamp(b) ||
  Date.parse(a.created_at) - Date.parse(b.created_at) ||
  a.id.localeCompare(b.id)

/** Order visible jobs by dependencies; timestamps break ties between ready jobs. */
export function orderRuns(items: JobRun[]) {
  const remaining = new Map(items.map(run => [run.id, run]))
  const edges = new Map(items.map(run => [run.id, new Set(run.depends_on)]))
  for (const run of items) {
    for (const child of run.downstream_jobs) edges.get(child)?.add(run.id)
  }
  const ordered: JobRun[] = []
  while (remaining.size) {
    const ready = [...remaining.values()].filter(
      run => ![...(edges.get(run.id) ?? [])].some(id => remaining.has(id))
    )
    // Malformed cyclic links must never hide a visible job.
    const next = (ready.length ? ready : [...remaining.values()]).sort(
      compareRuns
    )[0]
    if (!next) break
    ordered.push(next)
    remaining.delete(next.id)
  }
  return ordered
}

export function groupRuns(items: JobRun[]) {
  const parents = new Map<string, string>()
  function root(id: string): string {
    const parent = parents.get(id)
    if (!parent) {
      parents.set(id, id)
      return id
    }
    if (parent === id) return id
    const result = root(parent)
    parents.set(id, result)
    return result
  }
  function join(a: string, b: string) {
    parents.set(root(a), root(b))
  }
  for (const run of items) {
    root(run.id)
    if (run.routine_run_id) join(run.id, `routine:${run.routine_run_id}`)
    for (const id of [...run.depends_on, ...run.downstream_jobs])
      join(run.id, id)
  }
  const groups = new Map<string, JobRun[]>()
  for (const run of items) {
    const key = root(run.id)
    groups.set(key, [...(groups.get(key) ?? []), run])
  }
  const visibleIds = new Set(items.map(run => run.id))
  return [...groups.entries()]
    .map(([id, runs]) => ({
      id,
      runs: orderRuns(runs),
      missingIds: [
        ...new Set(
          runs.flatMap(run => [...run.depends_on, ...run.downstream_jobs])
        ),
      ].filter(id => !visibleIds.has(id)),
      newest: Math.max(...runs.map(run => Date.parse(run.created_at))),
    }))
    .sort((a, b) => b.newest - a.newest || a.id.localeCompare(b.id))
}
