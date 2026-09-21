import { X } from "lucide-react"
import { useRef } from "react"
import { useTranslation } from "react-i18next"
import { Dialog } from "./Dialog"

const companyDetails = [
  ["公司名稱", "廷豐金融科技股份有限公司（以下簡稱「本公司」）"],
  ["統一編號", "90596678"],
  ["登記地址", "台北市中山區樂群三路132號6樓"],
  ["客服電話", "(02)7728-7068"],
  ["服務時間", "週一至週五 08:30 - 17:30"],
  ["客服信箱", "first-pros01@fpitw.com"],
] as const

const takedownRequirements = [
  "聲明為受侵害著作權人或授權代理人。",
  "明確指出侵權內容之具體位置（務必提供具體 URL 及截圖）。",
  "說明原著名稱及原版權來源證明。",
  "真實姓名、電話及電子郵件。",
  "聲明確信該使用未經合法授權。",
  "聲明資訊真實並承擔法律責任（附電子/實體簽名）。",
]

function LegalSection({
  number,
  title,
  children,
}: {
  number: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section className="grid gap-5" aria-labelledby={`legal-section-${number}`}>
      <h3
        className="m-0 text-xl font-extrabold tracking-[-0.02em] text-sea-ink"
        id={`legal-section-${number}`}
      >
        {number}. {title}
      </h3>
      {children}
    </section>
  )
}

function LegalClause({
  number,
  title,
  children,
}: {
  number: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section className="grid gap-2">
      <h4 className="m-0 text-base font-extrabold text-sea-ink">
        {number} {title}
      </h4>
      <div className="grid gap-3 text-sm leading-7 text-sea-ink-soft">
        {children}
      </div>
    </section>
  )
}

export function LegalStatementDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const { t } = useTranslation()
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  return (
    <Dialog
      open={open}
      onClose={onClose}
      labelledBy="legal-statement-title"
      initialFocusRef={closeButtonRef}
      panelClassName="flex max-h-[min(88dvh,900px)] max-w-4xl flex-col overflow-hidden p-0"
    >
      <div className="flex shrink-0 items-center justify-between gap-4 border-b border-line px-5 py-4 sm:px-7">
        <div>
          <p className="eyebrow tracking-[0.08em] text-sea-ink-soft">
            Terms &amp; Privacy
          </p>
          <h2
            className="m-0 mt-1 text-xl font-extrabold tracking-[-0.02em] text-sea-ink"
            id="legal-statement-title"
          >
            法律聲明 (Legal Statement)
          </h2>
        </div>
        <button
          className="grid min-h-9 min-w-9 place-items-center rounded-md text-sea-ink-soft transition-colors hover:bg-link-hover hover:text-sea-ink"
          type="button"
          ref={closeButtonRef}
          onClick={onClose}
          aria-label={t("dismiss")}
        >
          <X className="size-4" aria-hidden="true" />
        </button>
      </div>

      <article className="overflow-y-auto overscroll-contain px-5 py-6 sm:px-7 sm:py-8">
        <div className="mx-auto grid max-w-3xl gap-9">
          <p className="m-0 text-sm leading-7 text-sea-ink-soft">
            歡迎您使用廷豐金融科技平台。為保障您的權益，並建立安全、合規的數位金融科技環境，請仔細閱讀以下政策條款。使用本平台服務，即表示您同意並接受以下所有規範。
          </p>

          <section className="grid gap-3" aria-labelledby="company-information">
            <h3
              className="m-0 text-base font-extrabold text-sea-ink"
              id="company-information"
            >
              公司基本資訊揭露
            </h3>
            <dl className="grid gap-x-5 gap-y-2 rounded-xl border border-line bg-subtle/45 p-4 text-sm sm:grid-cols-[auto_1fr]">
              {companyDetails.map(([term, description]) => (
                <div className="grid gap-1 sm:contents" key={term}>
                  <dt className="font-bold text-sea-ink">{term}</dt>
                  <dd className="m-0 break-words text-sea-ink-soft">
                    {description}
                  </dd>
                </div>
              ))}
            </dl>
          </section>

          <LegalSection number="1" title="使用者服務條款 (Terms of Service)">
            <LegalClause number="1.1" title="認知與接受條款">
              <p className="m-0">
                本公司係依據本服務條款提供各項金融科技、AI
                智慧資產配置工具及資訊聚合服務（以下簡稱「本服務」）。當您註冊、登入或實際使用本平台服務時，即表示您已閱讀、瞭解並同意接受本服務條款之所有內容。
              </p>
            </LegalClause>
            <LegalClause number="1.2" title="服務說明與投資風險聲明">
              <p className="m-0">
                本平台提供之所有 AI
                運算結果、市場數據、圖表及策略模型，皆僅供「研究與參考」用途。本平台之
                AI
                運算結果係基於歷史數據與演算法模型產生，不保證未來績效。本公司非屬法定之證券投資顧問事業，本服務內容絕不構成任何有價證券、衍生性金融商品或虛擬資產之買賣建議、邀約或投資勸誘。使用者應具備獨立判斷能力，任何依據本平台資訊所做出之交易決策，其投資風險與盈虧均由使用者自行承擔，本公司概不負責。
              </p>
            </LegalClause>
            <LegalClause number="1.3" title="第三方內容免責聲明">
              <p className="m-0">
                本平台所展示之部分金融資訊、市場數據、新聞標題及影音內容（包含但不限於
                YouTube
                影片），皆透過自動化資訊索引技術或官方應用程式介面（API）自第三方公開來源合法彙整與嵌入。本公司僅作為中立之資訊聚合與科技服務提供者，未對該等第三方內容進行實質編輯、修改或存放於本公司伺服器。
              </p>
              <p className="m-0">
                該等第三方內容之準確性、完整性、合法性及智慧財產權皆歸屬於原創作者、原發布媒體或授權平台。本公司不對第三方內容之真實性與合法性提供任何明示或默示之保證，亦不對使用者因依賴該等內容而產生之任何直接或間接交易損失承擔責任。使用者應自行評估資訊風險，並遵守原內容提供者之使用者條款。
              </p>
            </LegalClause>
            <LegalClause number="1.4" title="使用者行為規範與帳號安全">
              <p className="m-0">
                使用者應妥善保管其平台帳號及密碼，不得將帳號、密碼揭露或提供予第三人。因使用者未盡保管義務（如帳密外洩）所致之任何損失，由使用者自行承擔。
              </p>
              <p className="m-0">
                使用者承諾絕不為任何非法目的或以任何非法方式使用本服務，並遵守中華民國相關法規及網際網路之國際慣例。不得利用本服務從事侵害他人權益或違法之行為，包含：破壞系統安全、非法擷取數據（未經授權之爬蟲行為）、傳送誹謗、不實或違反公序良俗之內容。
              </p>
            </LegalClause>
            <LegalClause number="1.5" title="服務變更、暫停與終止">
              <p className="m-0">
                本公司保留隨時修改、暫停或終止本服務全部或一部之權利。如有影響使用者權益之重大變更，本公司將於平台首頁公告，並以電子郵件通知已註冊之使用者。
              </p>
            </LegalClause>
            <LegalClause number="1.6" title="疑義解釋原則">
              <p className="m-0">
                本條款如有疑義時，應為有利於使用者（消費者）之解釋。
              </p>
            </LegalClause>
            <LegalClause number="1.7" title="準據法與管轄法院">
              <p className="m-0">
                本條款之解釋與適用，以及與本條款有關之爭議，均應依照中華民國法律予以處理。因本條款涉訟時，雙方合意以台灣台北地方法院為第一審管轄法院。但若使用者為《消費者保護法》所稱之消費者，依消費者保護法及民事訴訟法相關規定，消費者仍得向其住所地法院提起訴訟。
              </p>
            </LegalClause>
          </LegalSection>

          <LegalSection number="2" title="隱私權政策 (Privacy Policy)">
            <LegalClause number="2.1" title="隱私權保護政策的適用範圍">
              <p className="m-0">
                本政策適用於您在使用廷豐金融科技平台時，所涉及的個人資料蒐集、處理與利用。不適用於本平台以外的第三方網站（如外部新聞網站或
                YouTube），亦不適用於非本公司委託或參與管理的人員。
              </p>
            </LegalClause>
            <LegalClause number="2.2" title="個人資料的蒐集、處理及利用方式">
              <p className="m-0">
                <strong className="text-sea-ink">蒐集目的：</strong>
                提供 AI 策略運算、帳戶管理、客戶服務、行銷通知及改善使用者體驗。
              </p>
              <p className="m-0">
                <strong className="text-sea-ink">蒐集類別：</strong>
                姓名、電子郵件、聯絡電話、設備資訊、IP 位址、瀏覽紀錄及與 AI
                互動之日誌數據。
              </p>
              <div>
                <p className="m-0 font-bold text-sea-ink">
                  利用期間、地區、對象及方式：
                </p>
                <ul className="m-0 mt-2 grid gap-2 pl-5 marker:text-lagoon">
                  <li>
                    <strong className="text-sea-ink">期間：</strong>
                    於本公司營運期間內，或特定服務目的完成後之合理期間內保存。期滿或依您請求時，將依法刪除或進行匿名化處理。
                  </li>
                  <li>
                    <strong className="text-sea-ink">地區：</strong>
                    台灣地區及本公司雲端伺服器所在地。
                  </li>
                  <li>
                    <strong className="text-sea-ink">對象：</strong>
                    本公司內部特定部門、委外之雲端服務供應商，及依法有權調查之機關。
                  </li>
                  <li>
                    <strong className="text-sea-ink">方式：</strong>
                    以自動化機器或其他非自動化之方式合法利用。
                  </li>
                </ul>
              </div>
              <p className="m-0">
                <strong className="text-sea-ink">自由選擇權：</strong>
                您得自由選擇是否提供上述個人資料，惟若您拒絕提供，可能導致無法完整使用本平台之各項服務。
              </p>
            </LegalClause>
            <LegalClause number="2.3" title="資料之保護">
              <p className="m-0">
                本平台主機設有防火牆、防毒系統等資訊安全設備及必要防護措施。僅有經過授權且簽署保密合約之人員方能接觸您的個人資料。
              </p>
            </LegalClause>
            <LegalClause number="2.4" title="第三方資料共享與連結">
              <p className="m-0">
                本公司不會任意出售、交換或出租您的個人資料給其他私人企業。但下列情形除外：經您書面/系統同意、配合司法/主管機關調查、基於委外業務需要（本公司將嚴格監督委外廠商之資安標準）。
              </p>
              <p className="m-0">
                (第三方服務聲明：本平台嵌入之 YouTube 等第三方服務，可能透過其
                API 蒐集觀看數據，此部分適用 Google / YouTube 之隱私權政策。)
              </p>
            </LegalClause>
            <LegalClause number="2.5" title="使用者權利行使">
              <p className="m-0">
                依據個人資料保護法，您可隨時透過專屬信箱（first-pros01@fpitw.com）請求行使：查詢或閱覽、製給複製本、補充或更正、停止蒐集/處理/利用、請求刪除。本公司將於收到請求後十五日內為准駁之決定，必要時得予延長（最長不逾十五日），並以書面或電子郵件通知原因。
              </p>
            </LegalClause>
            <LegalClause number="2.6" title="資料外洩通知義務">
              <p className="m-0">
                本公司如發生個人資料被竊取、洩漏、竄改或其他侵害情事，經查明後將於合理期間內以適當方式（如電子郵件或系統公告）通知受影響之當事人，並依法向主管機關通報。
              </p>
            </LegalClause>
          </LegalSection>

          <LegalSection number="3" title="智財權與侵權通報 (IP & Takedown)">
            <LegalClause number="3.1" title="智慧財產權宣告">
              <p className="m-0">
                廷豐金融科技平台上由本公司所產出之內容（含 AI
                演算法模型、視覺介面、商標、網站架構、自製圖表），智慧財產權均屬本公司所有。未經書面同意，不得逕自重製、改作或進行還原工程。
              </p>
              <p className="m-0">
                使用者於本平台輸入、上傳或產生之內容，其智慧財產權歸屬於使用者，惟使用者授予本公司於提供服務必要範圍內使用、儲存及展示該等內容之權利。使用者保證其上傳之內容未侵害第三人之智財權。
              </p>
            </LegalClause>
            <LegalClause number="3.2" title="著作權保護政策與侵權通報程序">
              <p className="m-0">
                本公司係屬《著作權法》第3條第1項所定之「資訊儲存服務提供者」及「搜尋服務提供者」。本公司尊重他人智財權，並依台灣著作權法第六章之一及國際著作權法令，建立版權保護機制。若您認為本平台展示或嵌入之內容侵害您的著作權，請透過專屬信箱提出正式通知。本公司接獲文件齊備之通知後，將於三個工作日內採取相應措施（移除連結或阻斷接取）。
              </p>
              <p className="m-0">
                <strong className="text-sea-ink">
                  重複侵權終止服務條款 (三振條款)：
                </strong>
                依據法規，若特定使用者於本平台經舉報涉有侵權情事達三次（含）以上，本公司得逕行終止該使用者之全部或部分服務。
              </p>
            </LegalClause>
            <LegalClause number="3.3" title="提出侵權通知必備要件">
              <p className="m-0">
                為加速處理，提出通知請備妥以下資訊送至 first-pros01@fpitw.com：
              </p>
              <ol className="m-0 grid gap-2 pl-5 marker:font-bold marker:text-lagoon">
                {takedownRequirements.map(requirement => (
                  <li key={requirement}>{requirement}</li>
                ))}
              </ol>
              <p className="m-0">
                (備援機制：若您發送信件後三個工作日內未收到本公司之自動回覆或處理確認，請於營業時間以電話聯繫本公司客服確認。)
              </p>
            </LegalClause>
            <LegalClause number="3.4" title="反向通知機制">
              <p className="m-0">
                若內容被移除之使用者（被檢舉人）認為其並無侵權情事，得向本公司提出回復通知。本公司將依法轉知原檢舉人；若原檢舉人未於法定期間內提出訴訟證明，本公司將於一定期間後恢復該內容之連結。
              </p>
            </LegalClause>
          </LegalSection>

          <LegalSection number="4" title="綜合條款 (Miscellaneous)">
            <LegalClause number="4.1" title="完整協議">
              <p className="m-0">
                本條款構成使用者與本公司間就本服務之完整協議，取代雙方先前所有口頭或書面之溝通、要約及陳述。
              </p>
            </LegalClause>
            <LegalClause number="4.2" title="版本控制與修改權利">
              <p className="m-0">
                本公司保留隨時修改本條款之權利。修改後之最新版本將公告於本平台，不另行個別通知。如使用者於條款更新後繼續使用本服務，即視為已閱讀並同意最新版本之規範。
              </p>
            </LegalClause>
            <LegalClause number="4.3" title="語言解釋">
              <p className="m-0">
                本條款如有提供英文或其他語言翻譯版本，僅供參考之用。若各語言版本間存在歧義或不一致之處，均以繁體中文版本為最終解釋基準。
              </p>
            </LegalClause>
          </LegalSection>
        </div>
      </article>
    </Dialog>
  )
}
