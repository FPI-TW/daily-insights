import { indexNameKey } from "#/lib/indices"

// Names the dashboards already use for the symbols they chart. Anything not
// listed (equities, most FX pairs, crypto beyond BTC/ETH) reads best as its
// own ticker, which is also what those dashboards show.
const dashboardNameKeys: Record<string, string> = {
  "^VIX": "reportLabelVix",
  "DX-Y.NYB": "macroAsset_dxy",
  "XBR/USD": "macroAsset_brent",
  "WTI/USD": "macroAsset_wti",
  "XAU/USD": "macroAsset_gold",
  "XAG/USD": "macroAsset_silver",
  HG1: "macroAsset_copper",
  TLT: "reportLabelTlt",
  IEF: "reportLabelIef",
  UUP: "reportLabelUup",
  "USD/TWD": "reportLabelUsdTwd",
  "USD/JPY": "reportLabelUsdJpy",
  "EUR/USD": "reportLabelEurUsd",
  "BTC/USD": "reportLabelBitcoin",
  "ETH/USD": "reportLabelEthereum",
}

/** The i18n key naming a related symbol, or undefined to show the symbol. */
export function relatedSymbolNameKey(symbol: string): string | undefined {
  return indexNameKey(symbol) ?? dashboardNameKeys[symbol]
}
