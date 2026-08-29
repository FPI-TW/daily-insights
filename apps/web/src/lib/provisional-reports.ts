export const marketCodes = [
  "global_macro_bonds",
  "crypto",
  "us_equity",
  "tw_equity",
  "tw_index_derivatives",
] as const

export type MarketCode = (typeof marketCodes)[number]
export type ReportStatus = "complete" | "partial" | "unavailable"
export type BlockStatus = "ok" | "missing" | "error"

export type ReportValue =
  | { kind: "literal"; value: string | number }
  | { kind: "translation"; key: string }

type Metric = {
  labelKey: string
  value: ReportValue | null
  change?: ReportValue | null
}

export type MetricBlock = {
  kind: "metric"
  status: BlockStatus
  titleKey: string
  metrics: ReadonlyArray<Metric>
}
export type TableBlock = {
  kind: "table"
  status: BlockStatus
  titleKey: string
  columns: ReadonlyArray<string>
  rows: ReadonlyArray<ReadonlyArray<ReportValue | null>>
}
export type SeriesBlock = {
  kind: "series"
  status: BlockStatus
  titleKey: string
  points: ReadonlyArray<{ label: ReportValue; value: number | null }>
}
export type ReportBlock = MetricBlock | TableBlock | SeriesBlock

export type ProvisionalReport = {
  marketCode: MarketCode
  status: ReportStatus
  editionDate: string
  sourceDate: string | null
  caveatKey: string
  blocks: ReadonlyArray<ReportBlock>
}

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
): TableBlock => ({ kind: "table", status, titleKey, columns, rows })
const series = (
  titleKey: string,
  points: SeriesBlock["points"],
  status: BlockStatus = "ok"
): SeriesBlock => ({ kind: "series", status, titleKey, points })

const reports: Record<MarketCode, ProvisionalReport> = {
  global_macro_bonds: {
    marketCode: "global_macro_bonds",
    status: "complete",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-27",
    caveatKey: "reportCaveatMock",
    blocks: [
      metric("reportBlockCommodities", [
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
        { labelKey: "reportLabelCopper", value: value("4.18"), change: null },
      ]),
      series("reportBlockTreasuryCurve", [
        { label: value("2Y"), value: 4.12 },
        { label: value("5Y"), value: 3.94 },
        { label: value("10Y"), value: 3.87 },
        { label: value("30Y"), value: 4.11 },
      ]),
      table(
        "reportBlockCredit",
        ["reportColumnInstrument", "reportColumnLevel", "reportColumnChange"],
        [
          [
            text("reportValueInvestmentGradeSpread"),
            text("reportValue92BasisPoints"),
            text("reportValueDown1BasisPoint"),
          ],
          [
            text("reportValueHighYieldSpread"),
            text("reportValue334BasisPoints"),
            text("reportValueUp4BasisPoints"),
          ],
        ]
      ),
    ],
  },
  crypto: {
    marketCode: "crypto",
    status: "partial",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-28",
    caveatKey: "reportCaveatPartial",
    blocks: [
      table(
        "reportBlockCryptoOverview",
        ["reportColumnAsset", "reportColumnPrice", "reportColumnChange"],
        [
          [value("BTC"), value("64,820"), value("+1.2%")],
          [value("ETH"), value("3,460"), value("+0.7%")],
          [value("XRP"), value("0.61"), null],
          [value("SOL"), value("154"), value("-0.5%")],
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
    ],
  },
  us_equity: {
    marketCode: "us_equity",
    status: "complete",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-28",
    caveatKey: "reportCaveatMock",
    blocks: [
      table(
        "reportBlockUsSectors",
        ["reportColumnSector", "reportColumnChange"],
        [
          [text("reportValueCommunicationServices"), value("+1.2%")],
          [text("reportValueConsumerDiscretionary"), value("+0.8%")],
          [text("reportValueConsumerStaples"), value("-0.3%")],
          [text("reportValueEnergy"), value("+1.7%")],
          [text("reportValueFinancials"), value("+0.4%")],
          [text("reportValueHealthCare"), value("-0.2%")],
          [text("reportValueIndustrials"), value("+0.6%")],
          [text("reportValueInformationTechnology"), value("+1.5%")],
          [text("reportValueMaterials"), value("+0.1%")],
          [text("reportValueRealEstate"), value("-0.7%")],
          [text("reportValueUtilities"), value("-0.4%")],
        ]
      ),
    ],
  },
  tw_equity: {
    marketCode: "tw_equity",
    status: "partial",
    editionDate: "2026-08-28",
    sourceDate: "2026-08-28",
    caveatKey: "reportCaveatPartial",
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
          change: null,
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
        ]
      ),
      metric(
        "reportBlockTechnicalSignals",
        [
          {
            labelKey: "reportLabelAbove20d",
            value: value("58%"),
            change: null,
          },
          { labelKey: "reportLabelRsi", value: null, change: null },
        ],
        "missing"
      ),
    ],
  },
  tw_index_derivatives: {
    marketCode: "tw_index_derivatives",
    status: "unavailable",
    editionDate: "2026-08-28",
    sourceDate: null,
    caveatKey: "reportCaveatUnavailable",
    blocks: [
      table(
        "reportBlockDerivativeQuotes",
        ["reportColumnInstrument", "reportColumnPrice", "reportColumnChange"],
        [
          [value("TX"), value("22,176"), value("+0.5%")],
          [value("MTX"), value("22,170"), value("+0.4%")],
          [value("TMF"), null, null],
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
    ],
  },
}

export async function getProvisionalReportList(): Promise<
  ReadonlyArray<ProvisionalReport>
> {
  return marketCodes.map(code => reports[code])
}

export async function getProvisionalReport(
  code: string
): Promise<ProvisionalReport | undefined> {
  return reports[code as MarketCode]
}
