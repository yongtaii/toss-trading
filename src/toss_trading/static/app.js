"use strict";
const $ = (id) => document.getElementById(id);
const money = (value) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(Number(value));
const number = (value) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(
    Number(value),
  );
const signedMoney = (value) =>
  value == null ? "—" : `${Number(value) > 0 ? "+" : ""}${money(value)}`;
const signedPercent = (value) =>
  new Intl.NumberFormat("ko-KR", {
    style: "percent",
    signDisplay: "exceptZero",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
const pnlClass = (value) =>
  Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
const time = (value) =>
  value
    ? new Intl.DateTimeFormat("ko-KR", {
        timeZone: "Asia/Seoul",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(new Date(value))
    : "기록 없음";
const labels = {
  BUY_FILL: "매수 체결",
  SELL_FILL: "매도 체결",
  WEEKLY_INTENT: "자동매매 주문 준비",
  LIVE_ORDER_SENDING: "증권사에 주문을 보내는 중",
  LIVE_ORDER_ACK: "증권사가 주문을 접수함",
  LIVE_ORDER_UNKNOWN: "주문 결과 확인 필요",
  LIVE_ORDER_ATTACHED: "증권사 주문을 거래 기록에 연결함",
  LIVE_ENTRY_BLOCKED: "새 매수 주문이 차단됨",
  LIVE_TRIM_EXPIRED: "보유 비중을 줄이는 주문 기한이 지남",
  LIVE_ENTRY_EXPIRED: "준비 중이던 매수 주문 기한이 지남",
  LIVE_RECONCILIATION_BLOCKED: "증권사 계좌 확인 실패",
  LIVE_CANCEL_SENDING: "증권사에 취소 요청을 보내는 중",
  LIVE_CANCEL_ACK: "증권사가 취소 요청을 접수함",
  BROKER_FEE_ADJUSTMENT: "증권사 수수료 반영",
  API_ERROR: "거래 정보 처리 오류",
  DAILY_LOSS_LIMIT: "하루 손실 한도 도달",
  ORDER_FAILED: "주문 처리 실패",
  REJECTED_DATA: "시세 데이터를 사용할 수 없음",
  EXIT_BLOCKED: "매도 주문이 차단됨",
  FLATTEN_UNRESOLVED: "보유 종목 정리 미완료",
  CANCEL_REQUESTED: "주문 취소 요청",
  CLOSED_OR_MISSING_CALENDAR: "정규장 밖 또는 시장 일정 미확인",
  DAILY_SUMMARY: "하루 거래 요약",
  DECISION: "자동매매 판단 기록",
  ORDER_SUBMITTED: "주문 제출",
  OVERNIGHT_POSITION: "장 마감 후에도 보유 중인 종목",
  PRIOR_SESSION_SEEDED: "직전 거래일 시세를 불러옴",
  RECONCILED: "증권사 계좌 확인 완료",
  STOP: "손절 기준 도달",
};
const orderStatuses = {
  SUBMITTED: "주문 제출됨",
  PARTIALLY_FILLED: "일부 수량 체결",
  FILLED: "전체 수량 체결",
  CANCELLED: "취소됨",
  REJECTED: "주문 거절됨",
  UNKNOWN: "주문 결과 확인 필요",
};
function reasonLabel(value) {
  return ({
    WEEKLY_ENTRY: "주간 전략 매수",
    WEEKLY_TRIM: "주간 목표 비중에 맞추기 위해 일부 매도",
    CLOSE_STOP_NEXT_OPEN: "마감 가격이 손절 기준에 도달",
    CLOSE_TARGET_NEXT_OPEN: "마감 가격이 목표가에 도달",
    TIME: "20거래일 보유",
    SIGNAL: "주간 상승 신호 해제",
    MANUAL_FLATTEN: "자동매매로 보유한 종목 매도",
    account_or_order_verification_failed: "증권사 계좌 또는 주문 확인 실패",
    live_poll_failed: "실거래 정보 조회 오류",
    cancellation_response_unknown: "취소 결과 확인 필요",
    closed_session_or_stale_quote: "정규장 밖이거나 시세가 오래됨",
    incomplete_or_outside_session: "시세가 불완전하거나 정규장 밖의 기록",
    not_allowlisted: "매매 대상에 등록되지 않은 종목",
    insufficient_cash: "주문에 필요한 현금 부족",
  })[value] || String(value);
}

let snapshot = null;
let view = "positions";
let controller;
let requestId = 0;

function tickMarketClock() {
  const market = snapshot?.market;
  if (!market) return;
  const target = market.is_open ? market.close : market.next_open;
  const remaining = Math.floor((Date.parse(target) - Date.now()) / 1000);
  $("market-clock-label").textContent = market.is_open
    ? "미국 정규장 마감까지"
    : "미국 정규장 개장까지";
  if (!target || remaining <= 0) {
    $("market-countdown").textContent = "시장 일정 갱신 대기";
    return;
  }
  const days = Math.floor(remaining / 86400);
  const hours = String(Math.floor((remaining % 86400) / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((remaining % 3600) / 60)).padStart(2, "0");
  const seconds = String(remaining % 60).padStart(2, "0");
  $("market-countdown").textContent =
    `${days ? days + "일 " : ""}${hours}:${minutes}:${seconds}`;
}
function renderHealth(data) {
  const worker = data.worker || { status: "unknown" };
  const states = {
    running: "실행 중",
    stopped: "종료됨",
    delayed: "작동 신호 지연",
    unknown: "실행 기록 없음",
  };
  $("worker-state").textContent = states[worker.status] || states.unknown;
  $("worker-state").className =
    worker.status === "running"
      ? "state-good"
      : worker.status === "delayed"
        ? "state-warning"
        : "state-muted";
  $("worker-phase").textContent =
    {
      starting: "시작 중",
      calendar: "시장 일정 조회",
      prior_session: "직전 거래일 시세 조회",
      candles: "분 단위 시세 조회",
      quotes: "매수·매도 가격 조회",
      processing: "매매 처리",
      idle: "대기 중",
      stopped: "종료",
    }[worker.phase] || "미확인";
  $("heartbeat").textContent = `마지막 작동 신호 ${time(worker.heartbeat_at)}`;
  const market = data.market;
  if (market) {
    const source = market.is_open ? market.source : market.next_open_source;
    $("market-source").textContent =
      source === "broker" ? "증권사 일정" : "시장 일정표 기준 예상";
    $("market-next-open").textContent =
      `${market.is_open ? "마감" : "다음 개장"} ${time(market.is_open ? market.close : market.next_open)} (한국 시간)`;
  }
  tickMarketClock();
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function svgEl(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes))
    node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}
function renderChart(trades) {
  const root = $("chart");
  root.replaceChildren();
  if (!trades.length) {
    root.append(el("p", "empty", "매도 거래가 완료되면 그래프가 표시됩니다."));
    return;
  }
  let cumulative = 0;
  const values = [0, ...trades.map((t) => (cumulative += Number(t.pnl)))];
  const low = Math.min(0, ...values),
    high = Math.max(0, ...values);
  const range = high - low || 1;
  const x = (i) => 68 + (i / (values.length - 1)) * 500;
  const y = (value) => 150 - ((value - low) / range) * 123;
  const svg = svgEl("svg", {
    viewBox: "0 0 590 188",
    role: "img",
    "aria-label": `매도 체결 ${trades.length}건, 누적 실현손익 ${money(cumulative)}`,
  });
  [0, 0.5, 1].forEach((fraction) => {
    const value = low + range * fraction;
    const height = y(value);
    svg.append(
      svgEl("line", {
        x1: 68,
        x2: 568,
        y1: height,
        y2: height,
        stroke: "#edf1f7",
        "stroke-dasharray": "3 4",
      }),
    );
    svg.append(
      svgEl(
        "text",
        { x: 57, y: height + 3, "text-anchor": "end" },
        money(value),
      ),
    );
  });
  const points = values.map((value, i) => `${x(i)},${y(value)}`).join(" ");
  svg.append(
    svgEl("polygon", {
      points: `${x(0)},165 ${points} ${x(values.length - 1)},165`,
      fill: "#3182f608",
    }),
  );
  svg.append(
    svgEl("polyline", {
      points,
      fill: "none",
      stroke: "#3182f6",
      "stroke-width": 2.5,
      "stroke-linejoin": "round",
    }),
  );
  svg.append(
    svgEl("circle", {
      cx: x(values.length - 1),
      cy: y(cumulative),
      r: 4,
      fill: "#3182f6",
      stroke: "white",
      "stroke-width": 2,
    }),
  );
  svg.append(svgEl("text", { x: 68, y: 183 }, "시작"));
  svg.append(
    svgEl(
      "text",
      { x: 568, y: 183, "text-anchor": "end" },
      time(trades.at(-1).closed),
    ),
  );
  root.append(svg);
}
function renderHoldings() {
  const root = $("holdings");
  root.replaceChildren();
  if (!snapshot?.positions.length) {
    root.append(el("p", "empty", snapshot
      ? "자동매매로 보유 중인 종목이 없습니다."
      : "거래 기록을 불러오는 중"));
    return;
  }
  const list = el("ul", "holdings-list");
  for (const p of snapshot.positions) {
    const quantity = Number(p.quantity);
    const cost = quantity * Number(p.entry) + Number(p.entry_fees ?? 0);
    const row = el("li", "holding");
    const information = el("div", "holding-information");
    const identity = el("div", "holding-identity");
    identity.append(
      el("strong", "", p.symbol),
      el("span", "holding-label", `${number(p.quantity)}주`),
    );

    const valuation = el("div", "holding-valuation");
    const value = p.mark == null ? null : quantity * Number(p.mark);
    const profit = p.unrealized;
    const rate = profit != null && cost > 0
      ? ` (${signedPercent(Number(profit) / cost)})`
      : "";
    const marketValue = el("div", "holding-metric");
    marketValue.append(
      el("span", "holding-label", "평가액"),
      el("strong", "holding-value", money(value)),
    );
    const profitValue = el("div", "holding-metric");
    profitValue.append(
      el("span", "holding-label", "평가 손익"),
      el("strong", pnlClass(profit), `${signedMoney(profit)}${rate}`),
    );
    valuation.append(marketValue, profitValue);

    const details = el("div", "holding-details");
    const thresholds = p.signal_symbol
      ? `${p.signal_symbol} ${money(p.signal_stop)} / ${money(p.signal_target)}`
      : `${money(p.stop)} / ${money(p.target)}`;
    details.append(
      el("span", "", `평균 매수가 ${money(p.entry)}`),
      el("span", "", `손절가 / 목표가 ${thresholds}`),
    );
    information.append(identity, details);
    row.append(information, valuation);
    list.append(row);
  }
  root.append(list);
}

function renderPortfolio() {
  const positions = view === "positions";
  $("positions-tab").classList.toggle("selected", positions);
  $("orders-tab").classList.toggle("selected", !positions);
  $("positions-tab").setAttribute("aria-pressed", String(positions));
  $("orders-tab").setAttribute("aria-pressed", String(!positions));
  $("holdings").hidden = !positions;
  $("order-table").hidden = positions;
  if (positions) {
    renderHoldings();
    return;
  }
  const titles = ["종목", "매수 / 매도", "주문 / 체결 수량", "주문 가격", "상태", "주문 기록 시각"];
  const head = el("tr");
  titles.forEach((title) => {
    const th = el("th", "", title);
    th.scope = "col";
    head.append(th);
  });
  $("portfolio-head").replaceChildren(head);
  const body = $("portfolio-body");
  body.replaceChildren();
  const rows = snapshot?.orders || [];
  if (!rows.length) {
    const row = el("tr");
    const cell = el("td", "empty", snapshot
      ? "기록된 실거래 주문이 없습니다."
      : "거래 기록을 불러오는 중");
    cell.colSpan = titles.length;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const p of rows) {
    const row = el("tr");
    const cells = [
      p.symbol,
      p.side === "BUY" ? "매수" : "매도",
      `${number(p.quantity)} / ${number(p.filled)}`,
      p.side === "SELL" ? "시장가" : money(p.limit),
      orderStatuses[p.status] || p.status,
      time(p.submitted),
    ];
    cells.forEach((value, i) => row.append(el("td", i === 0 ? "symbol" : "", value)));
    body.append(row);
  }
}
function renderEvents() {
  const filter = $("event-filter").value;
  const rows = (snapshot?.events || []).filter(
    (e) =>
      filter === "all" ||
      (filter === "fills"
        ? /^(BUY|SELL)_FILL$/.test(e.event)
        : /REJECTED|BLOCKED|ERROR|LIMIT|UNRESOLVED/.test(e.event)),
  );
  $("events").replaceChildren();
  if (!rows.length) {
    $("events").append(
      el(
        "p",
        "empty",
          snapshot ? "선택한 조건에 해당하는 활동이 없습니다." : "거래 기록을 불러오는 중",
      ),
    );
    return;
  }
  for (const record of rows) {
    const row = el("div", "event");
    row.append(
      el("span", "event-icon", record.event.includes("FILL") ? "↗" : "·"),
    );
    const description = el("div");
    description.append(
      el(
        "p",
        "event-title",
        `${record.symbol ? record.symbol + " · " : ""}${labels[record.event] || record.event}`,
      ),
    );
    const details = [];
    if (record.reason) details.push(reasonLabel(record.reason));
    if (record.reasons?.length)
      details.push(...record.reasons.map(reasonLabel));
    if (record.quantity != null) details.push(`${number(record.quantity)}주`);
    if (record.price != null) details.push(money(record.price));
    if (record.pnl != null) details.push(`손익 ${money(record.pnl)}`);
    if (record.signal)
      details.push(
        typeof record.signal === "string"
          ? record.signal
          : JSON.stringify(record.signal),
      );
    if (details.length)
      description.append(el("p", "event-reason", details.join(" · ")));
    row.append(description);
    const stamp = el("time", "", time(record.time));
    if (record.time) stamp.dateTime = record.time;
    row.append(stamp);
    $("events").append(row);
  }
}
function render(data) {
  snapshot = data;
  const summary = data.summary;
  const holdingsValue = Number(summary.equity) - Number(summary.cash);
  const holdingsPnl = Number(summary.unrealized);
  const holdingsCost = holdingsValue - holdingsPnl;
  $("equity").textContent = money(holdingsValue);
  $("cash").textContent = money(summary.cash);
  const totalPnl = $("total-pnl");
  const holdingsRate = holdingsCost > 0
    ? ` (${signedPercent(holdingsPnl / holdingsCost)})`
    : "";
  totalPnl.textContent = `${signedMoney(holdingsPnl)}${holdingsRate}`;
  totalPnl.className = pnlClass(holdingsPnl);
  $("reserved").textContent = `이 중 매수 주문 대기 금액 ${money(summary.reserved)}`;
  $("trade-count").textContent = number(data.trades.length);
  $("fees").textContent = money(summary.fees);
  const allocation = data.weekly_target_allocation;
  $("strategy").textContent =
    data.strategy === "weekly_baseline"
      ? allocation == null
        ? "주간 기본 전략"
        : `주간 전략 · 목표 비중 ${number(Number(allocation) * 100)}%`
      : data.strategy;
  $("account-check").textContent = data.reconciled
    ? `확인 완료 · ${time(data.account_verified_at)}`
    : "계좌 확인 대기 또는 실패";
  $("broker-buying-power").textContent = money(data.broker_buying_power);
  $("broker-buying-power").title = `마지막 계좌 확인 ${time(data.account_verified_at)}`;
  $("last-event").textContent = time(data.last_event);
  $("risk").textContent = data.loss_halted
    ? "한도 도달 · 신규 매수 중단"
    : "한도에 도달하지 않음";
  $("risk").className = data.loss_halted ? "positive" : "";
  const now = Date.now();
  const open =
    data.session &&
    now >= Date.parse(data.session.open) &&
    now < Date.parse(data.session.close);
  $("session").textContent = open ? "거래 시간" : "정규장 밖 또는 일정 미확인";
  $("engine-badge").textContent = {
    recent: "최근 기록 있음",
    delayed: "갱신 지연",
    unknown: "기록 없음",
  }[data.engine_status];
  const closed = data.market?.is_open === false && data.worker?.status === "running";
  $("notice").className =
    "notice" +
    ((!closed && data.engine_status === "delayed") || data.loss_halted ? " error" : "");
  $("notice").textContent =
    closed
      ? "미국 정규장 밖입니다. 마지막으로 저장된 잔액과 거래 내역을 표시합니다."
      : data.engine_status === "delayed"
      ? `마지막 매매 처리는 ${time(data.last_event)}입니다. 저장된 금액을 표시하고 있으니 자동매매 프로그램의 작동 상태를 확인하세요.`
      : data.engine_status === "unknown"
        ? "거래 기록을 불러왔습니다. 첫 정규장 매매 처리 기록을 기다리고 있습니다."
        : data.loss_halted
          ? "하루 손실 한도에 도달해 새 매수 주문이 중단됐습니다."
          : "총자산은 현금과 보유 종목의 평가액을 더한 금액입니다. 예상 매도 비용을 빼기 전 금액입니다.";
  renderHealth(data);
  renderChart(data.trades);
  renderPortfolio();
  renderEvents();
  $("updated").textContent = `${time(data.generated_at)} 조회 · 10초 자동 갱신`;
}
async function refresh() {
  const id = ++requestId;
  if (controller) controller.abort();
  controller = new AbortController();
  const activeController = controller;
  const timeout = setTimeout(() => activeController.abort(), 8000);
  $("refresh").disabled = true;
  try {
    const liveRequest = fetch("/dashboard/live-status", {
      signal: activeController.signal,
      cache: "no-store",
      credentials: "same-origin",
    })
      .then((response) => response.ok ? response.json() : null)
      .then((live) => {
        if (id !== requestId) return;
        $("live-status").textContent =
          live?.worker === "running"
            ? ({
                run: "실거래 · 신규 매수 허용",
                pause: "실거래 · 신규 매수 일시 중지",
                flatten: "실거래 · 보유 종목 매도 모드",
              }[live.control] || "실거래 · 주문 허용 상태를 확인할 수 없음")
            : ({
                unconfigured: "실거래 상태 조회 설정 없음",
                stopped: "실거래 · 자동매매 프로그램 종료",
                delayed: "실거래 · 작동 상태 갱신 지연",
              }[live?.worker] || "실거래 상태를 확인할 수 없음");
      })
      .catch(() => {
        if (id === requestId) $("live-status").textContent = "실거래 상태를 확인할 수 없음";
      });
    const [response] = await Promise.all([fetch(
      "/dashboard/data",
      {
        signal: activeController.signal,
        cache: "no-store",
        credentials: "same-origin",
      },
    ), liveRequest]);
    if (!response.ok)
      throw new Error(
        response.status === 503
          ? "실거래 기록을 불러오지 못했습니다. 자동매매 프로그램의 작동 상태와 거래 기록 설정을 확인하세요."
          : response.status === 401
            ? "인증이 만료됐습니다. 페이지를 다시 열어 로그인하세요."
            : `거래 기록을 불러오지 못했습니다 (${response.status}).`,
      );
    const data = await response.json();
    if (id === requestId) render(data);
  } catch (error) {
    if (id !== requestId) return;
    $("notice").className = "notice error";
    $("notice").textContent =
      (error.name === "AbortError"
        ? "조회 시간이 초과됐습니다."
        : error.message) + (snapshot ? " 마지막으로 불러온 정보를 표시하고 있습니다." : "");
    $("engine-badge").textContent = "조회 실패";
    $("updated").textContent = snapshot
      ? `마지막 성공 조회 ${time(snapshot.generated_at)}`
      : "연결 대기 · 10초 후 재시도";
  } finally {
    clearTimeout(timeout);
    if (id === requestId) $("refresh").disabled = false;
  }
}
function renderPreview() {
  const now = Date.now();
  const ago = (minutes) => new Date(now - minutes * 60000).toISOString();
  const session = { open: ago(60), close: ago(-120) };
  const positions = [
    {
      symbol: "QQQ", quantity: "10", entry: "200", entry_fees: "10",
      mark: "220", unrealized: "190", stop: "188", target: "240", mark_time: ago(0),
    },
    {
      symbol: "TLT", quantity: "10", entry: "144", entry_fees: "5",
      mark: "120", unrealized: "-245", stop: "115", target: "155", mark_time: ago(0),
    },
  ];
  render({
    summary: {
      equity: "10200", realized: "255", unrealized: "-55", cash: "6800",
      reserved: "180", total_pnl: "200", return_pct: "2", fees: "25",
    },
    positions,
    orders: [
      { symbol: "SPY", side: "BUY", quantity: 1, filled: 0, limit: 180, status: "SUBMITTED", submitted: ago(1) },
      { symbol: "QQQ", side: "BUY", quantity: 10, filled: 10, limit: 200, status: "FILLED", submitted: ago(4) },
    ],
    trades: [{ pnl: 125, closed: ago(50) }, { pnl: 130, closed: ago(25) }],
    events: [
      { event: "LIVE_ORDER_ACK", symbol: "SPY", time: ago(1) },
      { event: "BUY_FILL", symbol: "QQQ", time: ago(4), quantity: 10, price: 200 },
      { event: "SELL_FILL", symbol: "SPY", time: ago(25), quantity: 5, price: 205, pnl: 130 },
    ],
    strategy: "weekly_baseline",
    weekly_target_allocation: "0.30",
    reconciled: true,
    account_verified_at: ago(0),
    broker_buying_power: "6620",
    last_event: ago(0),
    generated_at: ago(0),
    engine_status: "recent",
    session,
    market: { ...session, is_open: true, source: "broker" },
    worker: {
      status: "running", phase: "idle", heartbeat_at: ago(0),
    },
  });
  $("live-status").textContent = "미리보기 · 샘플 데이터";
  $("notice").textContent = "UI 테스트용 가상 데이터입니다. 실제 잔액과 거래 내역은 실거래 화면에서 확인하세요.";
  $("updated").textContent = "샘플 데이터";
}

$("positions-tab").addEventListener("click", () => {
  view = "positions";
  renderPortfolio();
});
$("orders-tab").addEventListener("click", () => {
  view = "orders";
  renderPortfolio();
});
$("event-filter").addEventListener("change", renderEvents);
if (new URLSearchParams(location.search).get("preview") === "1") {
  $("preview-link").href = "/dashboard/";
  $("preview-link").textContent = "실거래 화면으로 돌아가기";
  $("refresh").addEventListener("click", renderPreview);
  renderPreview();
} else {
  $("refresh").addEventListener("click", refresh);
  renderPortfolio();
  refresh();
  setInterval(() => {
    if (!document.hidden) refresh();
  }, 10000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

setInterval(tickMarketClock, 1000);
