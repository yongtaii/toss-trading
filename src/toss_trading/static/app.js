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
  BUY_FILL: "실매수 체결",
  SELL_FILL: "실매도 체결",
  WEEKLY_INTENT: "전략 주문 준비",
  LIVE_ORDER_SENDING: "실주문 전송 중",
  LIVE_ORDER_ACK: "브로커 주문 접수",
  LIVE_ORDER_UNKNOWN: "주문 결과 확인 필요",
  LIVE_ORDER_ATTACHED: "브로커 주문 연결 완료",
  LIVE_ENTRY_BLOCKED: "신규 매수 차단",
  LIVE_TRIM_EXPIRED: "비중 조정 주문 만료",
  LIVE_ENTRY_EXPIRED: "매수 주문 준비 만료",
  LIVE_RECONCILIATION_BLOCKED: "계좌 대조 실패",
  LIVE_CANCEL_SENDING: "취소 전송 중",
  LIVE_CANCEL_ACK: "취소 요청 접수",
  BROKER_FEE_ADJUSTMENT: "브로커 수수료 정산",
};
function reasonLabel(value) {
  return ({
    WEEKLY_ENTRY: "주간 전략 매수",
    WEEKLY_TRIM: "주간 목표 비중으로 축소",
    CLOSE_STOP_NEXT_OPEN: "종가 손절 조건",
    CLOSE_TARGET_NEXT_OPEN: "종가 익절 조건",
    TIME: "20거래일 보유",
    SIGNAL: "주간 상승 신호 해제",
    MANUAL_FLATTEN: "관리 포지션 청산",
    account_or_order_verification_failed: "계좌 또는 주문 대조 실패",
    live_poll_failed: "LIVE 조회 오류",
    cancellation_response_unknown: "취소 결과 확인 필요",
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
            ? "현재 보유 중인 전략 포지션이 없습니다."
            : "기록된 실주문이 없습니다."
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
          p.signal_symbol
            ? `${p.signal_symbol} ${money(p.signal_stop)} / ${money(p.signal_target)}`
            : `${money(p.stop)} / ${money(p.target)}`,
        ]
      : [
          p.symbol,
          p.side === "BUY" ? "매수" : "매도",
          `${number(p.quantity)} / ${number(p.filled)}`,
          p.side === "SELL" ? "시장가" : money(p.limit),
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
  $("account-check").textContent = data.reconciled
    ? `대조 완료 · ${time(data.account_verified_at)}`
    : "대조 대기 또는 실패";
  $("broker-buying-power").textContent = money(data.broker_buying_power);
  $("broker-buying-power").title = `마지막 대조: ${time(data.account_verified_at)}`;
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
  const closed = data.market?.is_open === false && data.worker?.status === "running";
  $("notice").className =
    "notice" +
    ((!closed && data.engine_status === "delayed") || data.loss_halted ? " error" : "");
  $("notice").textContent =
    closed
      ? "미국 정규장 외 · 마지막으로 저장된 LIVE 잔고와 거래 내역입니다."
      : data.engine_status === "delayed"
      ? `엔진 마지막 기록: ${time(data.last_event)}. worker 실행 상태를 확인하세요. 표시된 금액은 마지막 저장 값입니다.`
      : data.engine_status === "unknown"
        ? "LIVE 원장이 연결됐습니다. 첫 정규장 처리 기록을 기다리고 있습니다."
        : data.loss_halted
          ? "일일 손실 한도에 도달해 신규 진입이 중단됐습니다."
          : "LIVE 원장 연결됨 · 총자산은 마지막 평가가 기준이며 예상 매도 비용은 차감 전입니다.";
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
                run: "LIVE · 실매수 허용",
                pause: "LIVE · 신규 매수 일시정지",
                flatten: "LIVE · 청산 모드",
              }[live.control] || "LIVE · 제어 상태 확인 불가")
            : ({
                unconfigured: "LIVE 상태 연결 안 됨",
                stopped: "LIVE · 실행기 정지",
                delayed: "LIVE · 상태 갱신 지연",
              }[live?.worker] || "LIVE 상태 확인 불가");
      })
      .catch(() => {
        if (id === requestId) $("live-status").textContent = "LIVE 상태 확인 불가";
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
          ? "LIVE 원장을 읽지 못했습니다. worker 실행 여부와 원장·전략 설정을 확인하세요."
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
