import { useEffect, useState } from "react"

export function useChartColors() {
  const [colors, setColors] = useState({
    series: [] as string[],
    text: "",
    grid: "",
  })

  useEffect(() => {
    const updateColors = () => {
      const styles = getComputedStyle(document.documentElement)
      setColors({
        series: [
          styles.getPropertyValue("--lagoon-deep").trim(),
          styles.getPropertyValue("--lagoon").trim(),
          styles.getPropertyValue("--market-up").trim(),
          styles.getPropertyValue("--market-down").trim(),
          styles.getPropertyValue("--market-caution").trim(),
        ],
        text: styles.getPropertyValue("--sea-ink-soft").trim(),
        grid: styles.getPropertyValue("--line").trim(),
      })
    }
    updateColors()
    const observer = new MutationObserver(updateColors)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    })
    return () => observer.disconnect()
  }, [])

  return colors
}
