// Illustrative report fixtures for component tests only. They must never be
// imported by application code: the customer UI renders API data exclusively.
import {
  launchMarketCodes,
  type MetricBlock,
  type ProvisionalReport,
  type ReportValue,
  type SeriesBlock,
  type TableBlock,
  type BlockStatus,
} from "#/lib/provisional-reports"

type FixtureCode =
  (typeof launchMarketCodes)[number] | "tw_equity" | "tw_index_derivatives"

const text = (key: string): ReportValue => ({ kind: "translation", key })
const value = (content: string | number): ReportValue => ({
  kind: "literal",
  value: content,
})
const metric = (
  titleKey: string,
  metrics: MetricBlock["metrics"],
  status: BlockStatus = "ok"
): MetricBlock => ({ kind: "metric", status, titleKey, metrics })
const table = (
  titleKey: string,
  columns: string[],
  rows: TableBlock["rows"],
  status: BlockStatus = "ok"
): TableBlock => ({
  kind: "table",
  status,
  titleKey,
  columns: columns.map(labelKey => ({ labelKey, unitCode: null })),
  rows,
})
const series = (
  titleKey: string,
  points: SeriesBlock["series"][number]["points"],
  status: BlockStatus = "ok"
): SeriesBlock => ({
  kind: "series",
  status,
  titleKey,
  series: [{ id: "primary", label: value(""), points }],
})

type FixtureReport = Omit<ProvisionalReport, "marketCode"> & {
  marketCode: FixtureCode
}

const reports: Record<FixtureCode, FixtureReport> = {
  global_macro_bonds: {
    marketCode: "global_macro_bonds",
    status: "complete",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-27",
    caveatKey: "reportCaveatMock",
    summaryKey: "reportSummary_global_macro_bonds",
    blocks: [
      metric("reportBlockMacroSnapshot", [
        {
          labelKey: "reportLabelBrent",
          value: value("72.40"),
          change: value("+0.8%"),
        },
        {
          labelKey: "reportLabelGold",
          value: value("2,418"),
          change: value("+0.3%"),
        },
        {
          labelKey: "reportLabelCopper",
          value: value("4.18"),
          change: value("-0.2%"),
        },
      ]),
      {
        kind: "series",
        status: "ok",
        titleKey: "reportBlockMacroCommodityNormalizedPerformance",
        series: [
          {
            id: "brent",
            label: value("Brent"),
            points: [
              { label: value("D1"), value: 100 },
              { label: value("D2"), value: 101.2 },
            ],
          },
          {
            id: "gold",
            label: value("Gold"),
            points: [
              { label: value("D1"), value: 100 },
              { label: value("D2"), value: 100.5 },
            ],
          },
        ],
      },
    ],
  },
  crypto: {
    marketCode: "crypto",
    status: "partial",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-28",
    caveatKey: "reportCaveatPartial",
    summaryKey: "reportSummary_crypto",
    blocks: [
      metric("reportBlockCryptoSnapshot", [
        {
          labelKey: "reportLabelBitcoin",
          value: value("64,820"),
          change: value("+1.2%"),
        },
        {
          labelKey: "reportLabelEthereum",
          value: value("3,460"),
          change: value("+0.7%"),
        },
        {
          labelKey: "reportLabelCryptoVolume",
          value: value("$82.4B"),
          change: value("-4.1%"),
        },
      ]),
      table(
        "reportBlockCryptoOverview",
        ["reportColumnAsset", "reportColumnPrice", "reportColumnChange"],
        [
          [value("BTC"), value("64,820"), value("+1.2%")],
          [value("ETH"), value("3,460"), value("+0.7%")],
          [value("SOL"), value("154"), value("-0.5%")],
          [value("XRP"), value("0.61"), value("+2.4%")],
          [value("ADA"), value("0.42"), value("-1.1%")],
        ]
      ),
      series("reportBlockNormalizedPerformance", [
        { label: text("reportAxisAug22"), value: 100 },
        { label: text("reportAxisAug25"), value: 103 },
        { label: text("reportAxisAug26"), value: null },
        { label: text("reportAxisAug27"), value: 101 },
        { label: text("reportAxisAug28"), value: 104 },
      ]),
      table(
        "reportBlockCryptoFlows",
        ["reportColumnInstrument", "reportColumnLevel", "reportColumnChange"],
        [
          [value("BTC ETF"), value("$164M"), value("+12M")],
          [value("ETH ETF"), value("$48M"), value("-9M")],
          [text("reportValueStablecoinSupply"), value("$176.2B"), null],
        ],
        "missing"
      ),
    ],
  },
  us_equity: {
    marketCode: "us_equity",
    status: "complete",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-28",
    caveatKey: "reportCaveatMock",
    summaryKey: "reportSummary_us_equity",
    blocks: [
      table(
        "reportBlockUsSectors",
        ["reportColumnSector", "reportColumnChange"],
        [
          [text("reportValueInformationTechnology"), value("+1.5%")],
          [text("reportValueEnergy"), value("+1.7%")],
          [text("reportValueCommunicationServices"), value("+1.2%")],
          [text("reportValueFinancials"), value("+0.4%")],
          [text("reportValueHealthCare"), value("-0.2%")],
          [text("reportValueRealEstate"), value("-0.7%")],
        ]
      ),
      series("reportBlockUsBreadth", [
        { label: text("reportAxisMonday"), value: 48 },
        { label: text("reportAxisTuesday"), value: 53 },
        { label: text("reportAxisWednesday"), value: 57 },
        { label: text("reportAxisThursday"), value: 55 },
        { label: text("reportAxisFriday"), value: 61 },
      ]),
      table(
        "reportBlockUsLeaders",
        ["reportColumnInstrument", "reportColumnPrice", "reportColumnChange"],
        [
          [value("NVDA"), value("128.42"), value("+2.8%")],
          [value("XOM"), value("116.20"), value("+2.0%")],
          [value("UNH"), value("495.10"), value("-1.4%")],
          [value("PLD"), value("123.58"), value("-1.1%")],
        ]
      ),
    ],
  },
  tw_equity: {
    preview: true,
    marketCode: "tw_equity",
    status: "partial",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-28",
    caveatKey: "reportCaveatPartial",
    summaryKey: "reportSummary_tw_equity",
    blocks: [
      metric("reportBlockTaiwanIndex", [
        {
          labelKey: "reportLabelTaiex",
          value: value("22,184"),
          change: value("+0.6%"),
        },
        {
          labelKey: "reportLabelTurnover",
          value: text("reportValue3412BillionTwd"),
          change: value("+8.4%"),
        },
        {
          labelKey: "reportLabelForeignFlow",
          value: text("reportValueForeignNetBuy1268"),
          change: text("reportValueForeignNetBuy381"),
        },
      ]),
      table(
        "reportBlockBreadth",
        ["reportColumnBreadth", "reportColumnCount"],
        [
          [text("reportValueAdvancers"), value(612)],
          [text("reportValueDecliners"), value(341)],
          [text("reportValueUnchanged"), value(97)],
        ]
      ),
      table(
        "reportBlockTaiwanSectors",
        ["reportColumnSector", "reportColumnChange"],
        [
          [text("reportValueSemiconductors"), value("+1.4%")],
          [text("reportValueFinancials"), value("+0.2%")],
          [text("reportValueShipping"), value("-0.8%")],
          [text("reportValueAiServers"), value("+1.7%")],
        ]
      ),
      series("reportBlockTaiwanTrend", [
        { label: value("09:00"), value: 22034 },
        { label: value("10:00"), value: 22081 },
        { label: value("11:00"), value: 22142 },
        { label: value("12:00"), value: 22115 },
        { label: value("13:30"), value: 22184 },
      ]),
      metric(
        "reportBlockTechnicalSignals",
        [
          {
            labelKey: "reportLabelAbove20d",
            value: value("58%"),
            change: value("+4ppt"),
          },
          { labelKey: "reportLabelRsi", value: null, change: null },
          {
            labelKey: "reportLabelMarketBreadth",
            value: value("1.79"),
            change: value("+0.12"),
          },
        ],
        "missing"
      ),
    ],
  },
  tw_index_derivatives: {
    preview: true,
    marketCode: "tw_index_derivatives",
    status: "unavailable",
    editionDate: "2026-08-28",
    sourceDate: null,
    caveatKey: "reportCaveatUnavailable",
    summaryKey: "reportSummary_tw_index_derivatives",
    blocks: [
      metric(
        "reportBlockDerivativeSnapshot",
        [
          {
            labelKey: "reportLabelTx",
            value: value("22,176"),
            change: value("+0.5%"),
          },
          {
            labelKey: "reportLabelMtx",
            value: value("22,170"),
            change: value("+0.4%"),
          },
          {
            labelKey: "reportLabelPutCall",
            value: value("0.93"),
            change: null,
          },
        ],
        "missing"
      ),
      table(
        "reportBlockDerivativeQuotes",
        ["reportColumnInstrument", "reportColumnPrice", "reportColumnChange"],
        [
          [value("TX"), value("22,176"), value("+0.5%")],
          [value("MTX"), value("22,170"), value("+0.4%")],
          [value("TMF"), null, null],
          [value("TXO 22000P"), value("218"), value("-8.4%")],
        ],
        "missing"
      ),
      series(
        "reportBlockPositioning",
        [
          { label: text("reportAxisPercentile20"), value: 22 },
          { label: text("reportAxisPercentile40"), value: 38 },
          { label: text("reportAxisPercentile60"), value: null },
          { label: text("reportAxisPercentile80"), value: 71 },
          { label: text("reportAxisCurrent"), value: 64 },
        ],
        "error"
      ),
      table(
        "reportBlockDerivativeOpenInterest",
        ["reportColumnInstrument", "reportColumnLevel", "reportColumnChange"],
        [
          [text("reportValueForeignTxNetPosition"), value("+18,426"), null],
          [
            text("reportValueInvestmentTrustTxNetPosition"),
            value("-2,131"),
            null,
          ],
          [text("reportValueOptionsPcr"), value("0.93"), value("-0.04")],
        ],
        "missing"
      ),
    ],
  },
}

export async function getProvisionalReportList(): Promise<
  ReadonlyArray<ProvisionalReport>
> {
  return launchMarketCodes.map(code => reports[code] as ProvisionalReport)
}
export async function getProvisionalReport(
  code: string
): Promise<ProvisionalReport | undefined> {
  return reports[code as FixtureCode] as ProvisionalReport | undefined
}
