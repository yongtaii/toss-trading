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
  BUY_FILL: "모의 매수 체결",
  SELL_FILL: "모의 매도 체결",
  ORDER_SUBMITTED: "주문 제출",
  ENTRY_REJECTED: "신규 진입 차단",
  DECISION: "전략 판단",
  API_ERROR: "시장 데이터 조회 오류",
  DAILY_LOSS_LIMIT: "일일 손실 한도 도달",
  STOP: "손절",
  DAILY_SUMMARY: "일일 결과",
  CLOSED_OR_MISSING_CALENDAR: "장외 또는 시장 일정 없음",
  WARMUP_FALLBACK: "당일 지표로 워밍업",
  PRIOR_SESSION_SEEDED: "전일 지표 준비 완료",
  REJECTED_DATA: "데이터 검증 거부",
  EXIT_BLOCKED: "청산 대기",
  FLATTEN_UNRESOLVED: "마감 청산 미완료",
  CANCELLED: "주문 취소",
  RECONCILED: "원장 준비 완료",
  OPPOSING_GROUP: "반대 방향 그룹 보유",
  GROUP_RISK_LIMIT: "그룹 위험 한도",
  VOLUME_HISTORY_INSUFFICIENT: "시간대 거래량 이력 부족",
};
const checkLabels = {
  complete_contiguous_session: "정규장 분봉 연속성",
  warmup: "지표 워밍업",
  volume_available: "거래량 데이터",
  stretched_below_vwap: "VWAP 이탈 폭",
  after_open: "개장 후 대기 시간",
  before_close: "마감 전 진입 시간",
  atr_positive: "유효한 ATR",
  volume: "거래량 조건",
  rsi: "RSI 조건",
  vwap_cross_up: "VWAP 상향 돌파",
  reconciled: "원장 준비",
  no_overnight_positions: "전일 잔여 포지션 없음",
  regular_session: "정규장",
  entry_window: "진입 가능 시간",
  not_held: "동일 종목 미보유",
  no_pending: "동일 종목 미체결 없음",
  loss_limit: "일일 손실 한도",
  slots: "포지션 여유",
  opposing_group: "반대 방향 그룹 미보유",
  group_risk: "그룹 위험 여유",
  entry_count: "종목별 진입 횟수",
  cooldown: "쿨다운 종료",
  all_position_marks_fresh: "보유 종목 평가가 유효",
  fresh_symbol_quote: "해당 종목 시세 유효",
  worker_running: "수집기 heartbeat",
  market_data_ready: "최근 시세 수집 성공",
  entries_enabled: "수집 오류 없이 진입 허용",
  fresh_bar: "신호 분봉 유효",
  quote_available: "호가 존재",
  allowlisted: "거래 허용 종목",
  atr: "ATR 대비 가격 유효",
  fresh_quote: "호가 유효 시간",
  spread: "스프레드 한도",
  cost: "비용 한도",
  minimum_quantity: "최소 주문 수량",
  new_signal: "미처리 신호",
};
const preciseMoney = (value) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      }).format(Number(value));
function reasonLabel(value) {
  if (value === "OPPOSING_GROUP") return "반대 방향 그룹 보유";
  if (value === "GROUP_RISK_LIMIT") return "그룹 위험 한도 부족";
  const key = String(value).toLowerCase();
  const reasons = {
    complete_contiguous_session: "정규장 분봉 누락",
    warmup: "지표 워밍업 중",
    volume_available: "거래량 데이터 부족",
    stretched_below_vwap: "VWAP 이탈 폭 부족",
    after_open: "개장 후 대기 시간",
    before_close: "마감 전 신규 진입 중단",
    atr_positive: "유효한 ATR 대기",
    volume: "거래량 조건 미충족",
    rsi: "RSI 조건 미충족",
    vwap_cross_up: "VWAP 상향 돌파 대기",
    reconciled: "원장 준비 대기",
    no_overnight_positions: "전일 잔여 포지션 확인 필요",
    regular_session: "정규장 외",
    entry_window: "진입 허용 시간 밖",
    not_held: "이미 보유 중",
    no_pending: "미체결 주문 대기",
    loss_limit: "일일 손실 한도 조건 미충족",
    slots: "최대 포지션 수 도달",
    opposing_group: "반대 방향 그룹 보유",
    group_risk: "그룹 위험 여유 부족",
    entry_count: "오늘 진입 횟수 한도",
    cooldown: "쿨다운 진행 중",
    all_position_marks_fresh: "보유 종목 평가가 갱신 필요",
    fresh_symbol_quote: "종목 시세 없음 또는 갱신 지연",
    worker_running: "수집기 heartbeat 확인 필요",
    market_data_ready: "최신 시세 수집 확인 필요",
    entries_enabled: "수집 문제로 신규 진입 중단",
    fresh_bar: "신호 분봉 갱신 필요",
    quote_available: "호가 대기",
    fresh_quote: "호가 갱신 필요",
    new_signal: "이미 처리한 신호",
  };
  if (reasons[key]) return reasons[key];
  if (checkLabels[key]) return checkLabels[key] + " 미충족";
  const messages = {
    "Calendar unavailable; new entries suspended":
      "시장 일정 조회 실패 · 신규 진입 중단",
    "Market data or calendar unavailable; new entries suspended":
      "시세 또는 시장 일정 조회 실패 · 신규 진입 중단",
    "Prior session candles unavailable; intraday-only warmup":
      "전일 분봉 조회 실패 · 당일 데이터로 워밍업",
    "candles unavailable": "분봉 조회 실패",
    "orderbook unavailable": "호가 조회 실패",
    "candles missing or stale": "분봉 누락 또는 갱신 지연",
    "orderbook missing or stale": "호가 누락 또는 갱신 지연",
  };
  if (messages[value]) return messages[value];
  const parts = String(value).match(/^([A-Z]+): (.+)$/);
  return parts
    ? `${parts[1]} · ${messages[parts[2]] || parts[2]}`
    : String(value);
}
function tickMarketClock() {
  const market = snapshot?.market;
  if (!market) return;
  const target = market.is_open ? market.close : market.next_open;
  const remaining = Math.floor((Date.parse(target) - Date.now()) / 1000);
  $("market-clock-label").textContent = market.is_open
    ? "정규장 마감까지"
    : "다음 개장까지";
  if (!target || remaining <= 0) {
    $("market-countdown").textContent = "일정 갱신 대기";
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
    delayed: "heartbeat 지연",
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
      prior_session: "전일 분봉 조회",
      candles: "분봉 조회",
      quotes: "호가 조회",
      processing: "매매 처리",
      idle: "다음 수집 대기",
      stopped: "종료",
    }[worker.phase] || "미확인";
  $("heartbeat").textContent = `마지막 heartbeat ${time(worker.heartbeat_at)}`;
  $("feed-state").textContent =
    {
      success: "최근 수집 성공",
      error: "수집 오류",
      stale: "데이터 갱신 지연",
      closed: "장외 · 수집 대기",
    }[worker.market_data_status] || "수집 기록 없음";
  $("feed-state").className = ["error", "stale"].includes(
    worker.market_data_status,
  )
    ? "state-warning"
    : "";
  $("feed-badge").textContent = worker.last_poll_finished
    ? "최근 수집 결과"
    : "미확인";
  $("feed-last-success").textContent =
    `시세 마지막 성공 ${time(worker.last_market_data_success)}`;
  $("calendar-last-success").textContent =
    `시장 일정 조회 성공 ${time(worker.last_calendar_success)}`;
  const market = data.market;
  if (market) {
    const source = market.is_open ? market.source : market.next_open_source;
    $("market-source").textContent =
      source === "broker" ? "증권사 일정" : "로컬 캘린더 예상";
    $("market-next-open").textContent =
      `${market.is_open ? "마감" : "다음 개장"} ${time(market.is_open ? market.close : market.next_open)} KST`;
  }
  const error = worker.last_error;
  $("collector-error").hidden = !error;
  if (error) {
    const active = Boolean(worker.errors?.length);
    $("collector-error").textContent =
      `${active ? "수집 문제" : "최근 수집 문제 · 다음 수집에서 복구됨"} (${time(error.time)}) · ${(error.reasons || []).map(reasonLabel).join(" / ")}`;
  }
  tickMarketClock();
}
function renderEntryConditions(rows = []) {
  const root = $("entry-conditions");
  root.replaceChildren();
  if (!rows.length) {
    root.append(el("p", "empty", "전략 판단 기록을 기다리고 있습니다."));
    return;
  }
  const statuses = {
    closed: "장외 대기",
    warming_up: "데이터 준비 중",
    stale: "판단 기록 지연",
    blocked: "진입 차단",
    waiting: "조건 대기",
    signal: "매수 신호 기록",
  };
  for (const row of rows) {
    const card = el("article", "entry-card");
    const top = el("div", "entry-top");
    top.append(
      el("span", "entry-name", row.symbol),
      el("span", `entry-status ${row.status}`, statuses[row.status]),
    );
    card.append(top);
    let reason;
    if (row.status === "closed")
      reason = "정규장 밖에서는 신규 진입하지 않습니다.";
    else if (!row.gates.worker_running)
      reason = "수집기 실행 상태를 확인하세요.";
    else if (!row.decision_fresh)
      reason =
        row.indicators?.close != null
          ? "새로운 5분봉 판단을 기다리고 있습니다."
          : row.failed_checks?.length
            ? row.failed_checks.map(reasonLabel).join(" · ")
            : "정규장 분봉과 지표 준비를 기다리고 있습니다.";
    else if (row.failed_gates.length)
      reason = row.failed_gates.map(reasonLabel).join(" · ");
    else if (row.failed_checks.length)
      reason = row.failed_checks.map(reasonLabel).join(" · ");
    else
      reason =
        row.status === "signal"
          ? "매수 신호가 기록됐습니다. 주문 수량·호가·비용은 엔진에서 최종 검증합니다."
          : "다음 진입 신호를 기다리고 있습니다.";
    card.append(
      el("p", "entry-reason", reason),
      el(
        "p",
        "entry-at",
        `5분봉 판단 ${time(row.decision_at)}${row.decision_at && !row.decision_fresh ? " · 이전 기록" : ""}`,
      ),
    );
    const values = el("dl", "entry-values");
    for (const [label, value] of [
      ["5분봉 종가", row.indicators.close],
      ["당일 VWAP", row.indicators.vwap],
      ["ATR · 5분봉", row.indicators.atr],
      ["진입 기준 · 종가가 이 값 미만", row.entry_below],
    ]) {
      const item = el("div");
      item.append(el("dt", "", label), el("dd", "", preciseMoney(value)));
      values.append(item);
    }
    card.append(values);
    const distance = row.distance_to_entry;
    card.append(
      el(
        "p",
        "entry-distance",
        distance == null
          ? "진입 기준 계산 대기"
          : Number(row.indicators.close) < Number(row.entry_below)
            ? "마지막 5분봉 종가의 VWAP 이탈 폭 충족"
            : Number(distance) === 0
              ? "진입 경계값 도달 · 종가가 기준보다 낮아야 충족"
              : `마지막 5분봉 종가에서 ${preciseMoney(distance)} 초과 하락 시 가격 조건 충족`,
      ),
    );
    const detail = el("details", "entry-detail");
    detail.append(el("summary", "", "조건별 결과와 마지막 실행 판단"));
    for (const [heading, checks] of [
      ["전략 조건 · 마지막 5분봉", row.checks],
      ["원장·시세 조건 · 현재 조회 기준", row.gates],
    ]) {
      const group = el("div", "check-group");
      group.append(el("h3", "", heading));
      if (!Object.keys(checks).length)
        group.append(el("p", "entry-last", "판단 기록 없음"));
      for (const [key, passed] of Object.entries(checks)) {
        const line = el("div", `check-row ${passed ? "pass" : "fail"}`);
        line.append(
          el("span", "", checkLabels[key] || key),
          el("span", "", passed ? "충족" : "미충족"),
        );
        group.append(line);
      }
      detail.append(group);
    }
    if (row.cooldown_until)
      detail.append(
        el("p", "entry-last", `쿨다운 종료 ${time(row.cooldown_until)}`),
      );
    if (row.last_entry)
      detail.append(
        el(
          "p",
          "entry-last",
          `마지막 실행 판단 ${time(row.last_entry.time)} · ${labels[row.last_entry.event] || row.last_entry.event}${row.last_entry.reasons?.length ? " · " + row.last_entry.reasons.map(reasonLabel).join(" / ") : ""}`,
        ),
      );
    card.append(detail);
    root.append(card);
  }
}

let snapshot = null;
let view = "positions";
let controller;
let requestId = 0;
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function setMoney(id, value, colored = false) {
  const node = $(id);
  node.textContent = money(value);
  node.className = colored
    ? Number(value) > 0
      ? "positive"
      : Number(value) < 0
        ? "negative"
        : ""
    : "";
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
    root.append(el("p", "empty", "완료된 매매가 쌓이면 그래프가 표시됩니다."));
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
function renderPortfolio() {
  const positions = view === "positions";
  $("positions-tab").classList.toggle("selected", positions);
  $("orders-tab").classList.toggle("selected", !positions);
  $("positions-tab").setAttribute("aria-pressed", String(positions));
  $("orders-tab").setAttribute("aria-pressed", String(!positions));
  const titles = positions
    ? [
        "종목",
        "보유 수량",
        "평균 진입가",
        "평가가",
        "미실현손익",
        "손절 / 목표가",
      ]
    : ["종목", "방향", "주문 / 체결 수량", "지정가", "상태", "제출 시각"];
  const head = el("tr");
  titles.forEach((title) => {
    const th = el("th", "", title);
    th.scope = "col";
    head.append(th);
  });
  $("portfolio-head").replaceChildren(head);
  const body = $("portfolio-body");
  body.replaceChildren();
  const rows = snapshot
    ? positions
      ? snapshot.positions
      : snapshot.orders
    : [];
  if (!rows.length) {
    const row = el("tr"),
      cell = el(
        "td",
        "empty",
        snapshot
          ? positions
            ? "현재 보유 중인 모의 포지션이 없습니다."
            : "체결 대기 중인 모의 주문이 없습니다."
          : "원장 연결 대기",
      );
    cell.colSpan = titles.length;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const p of rows) {
    const row = el("tr");
    const cells = positions
      ? [
          p.symbol,
          number(p.quantity),
          money(p.entry),
          money(p.mark),
          money(p.unrealized),
          `${money(p.stop)} / ${money(p.target)}`,
        ]
      : [
          p.symbol,
          p.side === "BUY" ? "매수" : "매도",
          `${number(p.quantity)} / ${number(p.filled)}`,
          money(p.limit),
          p.status,
          time(p.submitted),
        ];
    cells.forEach((value, i) => {
      const cell = el("td", i === 0 ? "symbol" : "", value);
      if (positions && i === 4)
        cell.className =
          Number(p.unrealized) > 0
            ? "positive"
            : Number(p.unrealized) < 0
              ? "negative"
              : "";
      if (positions && i === 3) cell.title = `평가 시각: ${time(p.mark_time)}`;
      row.append(cell);
    });
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
        snapshot ? "선택한 조건의 활동이 없습니다." : "원장 연결 대기",
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
  setMoney("equity", summary.equity);
  setMoney("realized", summary.realized, true);
  setMoney("unrealized", summary.unrealized, true);
  setMoney("cash", summary.cash);
  $("total-pnl").textContent =
    `시작 대비 ${money(summary.total_pnl)} (${Number(summary.return_pct) >= 0 ? "+" : ""}${Number(summary.return_pct).toFixed(2)}%)`;
  $("reserved").textContent = `주문 예약금 ${money(summary.reserved)}`;
  $("trade-count").textContent = number(data.trades.length);
  $("fees").textContent = money(summary.fees);
  $("strategy").textContent = data.strategy;
  $("last-event").textContent = time(data.last_event);
  $("risk").textContent = data.loss_halted
    ? "일일 손실 한도 · 진입 중단"
    : "일일 손실 한도 미도달";
  $("risk").className = data.loss_halted ? "positive" : "";
  const now = Date.now();
  const open =
    data.session &&
    now >= Date.parse(data.session.open) &&
    now < Date.parse(data.session.close);
  $("session").textContent = open ? "미국 정규장" : "정규장 외 / 세션 미확인";
  $("engine-badge").textContent = {
    recent: "최근 기록 있음",
    delayed: "갱신 지연",
    unknown: "기록 없음",
  }[data.engine_status];
  $("notice").className =
    "notice" +
    (data.engine_status === "delayed" || data.loss_halted ? " error" : "");
  $("notice").textContent =
    data.engine_status === "delayed"
      ? `엔진 마지막 기록: ${time(data.last_event)}. worker 실행 상태를 확인하세요. 표시된 금액은 마지막 저장 값입니다.`
      : data.engine_status === "unknown"
        ? "원장은 연결됐지만 엔진 실행 기록이 없습니다. PAPER worker를 실행하면 데이터가 갱신됩니다."
        : data.loss_halted
          ? "일일 손실 한도에 도달해 신규 진입이 중단됐습니다."
          : "PAPER 원장 연결됨 · 총자산은 마지막 평가가 기준이며 예상 매도 비용은 차감 전입니다.";
  const compareOption = $("ledger").options[1];
  compareOption.disabled = !data.compare_available;
  compareOption.textContent = data.compare_available
    ? "비교 전략"
    : "비교 전략 · 미설정";
  $("watchlist").replaceChildren();
  for (const ticker of data.symbols) {
    const card = el("article", "ticker");
    card.append(el("span", "ticker-icon", ticker.symbol.slice(0, 2)));
    const name = el("div");
    name.append(el("strong", "", ticker.symbol), el("small", "", "관찰 종목"));
    const value = el("div", "ticker-value", money(ticker.mark));
    const age = ticker.mark_time
      ? (now - Date.parse(ticker.mark_time)) / 1000
      : null;
    value.append(
      el(
        "small",
        "",
        age == null
          ? "시세 기록 없음"
          : `${age > 90 ? "이전 시세 · " : ""}${time(ticker.mark_time)}`,
      ),
    );
    card.append(name, value);
    $("watchlist").append(card);
  }
  renderHealth(data);
  renderEntryConditions(data.entry_conditions);
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
    const response = await fetch(
      `/dashboard/data?ledger=${encodeURIComponent($("ledger").value)}`,
      {
        signal: activeController.signal,
        cache: "no-store",
        credentials: "same-origin",
      },
    );
    if (!response.ok)
      throw new Error(
        response.status === 503
          ? "PAPER 원장을 읽지 못했습니다. worker 실행 여부와 원장·전략 설정을 확인하세요."
          : response.status === 401
            ? "인증이 만료됐습니다. 페이지를 다시 열어 로그인하세요."
            : `원장 조회에 실패했습니다 (${response.status}).`,
      );
    const data = await response.json();
    if (id === requestId) render(data);
  } catch (error) {
    if (id !== requestId) return;
    $("notice").className = "notice error";
    $("notice").textContent =
      (error.name === "AbortError"
        ? "조회 시간이 초과됐습니다."
        : error.message) + (snapshot ? " 화면은 이전 조회 결과입니다." : "");
    $("engine-badge").textContent = "조회 실패";
    $("updated").textContent = snapshot
      ? `마지막 성공 조회 ${time(snapshot.generated_at)}`
      : "연결 대기 · 10초 후 재시도";
  } finally {
    clearTimeout(timeout);
    if (id === requestId) $("refresh").disabled = false;
  }
}
$("refresh").addEventListener("click", refresh);
$("ledger").addEventListener("change", () => {
  snapshot = null;
  for (const id of [
    "equity",
    "realized",
    "unrealized",
    "cash",
    "total-pnl",
    "reserved",
    "strategy",
    "session",
    "last-event",
    "risk",
    "trade-count",
    "fees",
    "worker-state",
    "worker-phase",
    "heartbeat",
    "feed-state",
    "feed-badge",
    "feed-last-success",
    "calendar-last-success",
    "market-countdown",
    "market-next-open",
    "market-source",
  ])
    $(id).textContent = "—";
  $("chart").replaceChildren(
    el("p", "empty", "선택한 원장을 불러오고 있습니다."),
  );
  $("watchlist").replaceChildren();
  $("collector-error").hidden = true;
  renderEntryConditions();
  renderPortfolio();
  renderEvents();
  refresh();
});
$("positions-tab").addEventListener("click", () => {
  view = "positions";
  renderPortfolio();
});
$("orders-tab").addEventListener("click", () => {
  view = "orders";
  renderPortfolio();
});
$("event-filter").addEventListener("change", renderEvents);
renderPortfolio();
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 10000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});

setInterval(tickMarketClock, 1000);
