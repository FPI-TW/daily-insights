import { useEffect, useState } from "react"

export function useChartColors() {
  const [colors, setColors] = useState({
    series: [] as string[],
    indexSeries: [] as string[],
    text: "",
    grid: "",
    gridSoft: "",
    faint: "",
    surface: "",
    chipLine: "",
    up: "",
    down: "",
    font: "",
  })

  useEffect(() => {
    const updateColors = () => {
      const styles = getComputedStyle(document.documentElement)
      setColors({
        series: [
          styles.getPropertyValue("--chart-1").trim(),
          styles.getPropertyValue("--chart-2").trim(),
          styles.getPropertyValue("--chart-3").trim(),
          styles.getPropertyValue("--chart-4").trim(),
          styles.getPropertyValue("--chart-5").trim(),
        ],
        indexSeries: [
          styles.getPropertyValue("--chart-index-close").trim(),
          styles.getPropertyValue("--chart-index-sma-20").trim(),
          styles.getPropertyValue("--chart-index-sma-60").trim(),
          styles.getPropertyValue("--chart-index-sma-120").trim(),
          styles.getPropertyValue("--chart-index-sma-240").trim(),
        ],
        text: styles.getPropertyValue("--sea-ink-soft").trim(),
        grid: styles.getPropertyValue("--line").trim(),
        gridSoft: styles.getPropertyValue("--line-soft").trim(),
        faint: styles.getPropertyValue("--sea-ink-faint").trim(),
        surface: styles.getPropertyValue("--surface").trim(),
        chipLine: styles.getPropertyValue("--chip-line").trim(),
        up: styles.getPropertyValue("--market-up").trim(),
        down: styles.getPropertyValue("--market-down").trim(),
        font: getComputedStyle(document.body).fontFamily,
      })
    }
    updateColors()
    const observer = new MutationObserver(updateColors)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme", "lang"],
    })
    return () => observer.disconnect()
  }, [])

  return colors
}
