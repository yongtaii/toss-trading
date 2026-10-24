# 미국 주식 단타 전략 구현 보고

## 1. 기존 코드 분석과 구현 범위

기존 프로젝트는 `TossQueries`의 OAuth·시세/계좌/잔고/주문 조회, FastAPI 인증, HTTP 주문 차단,
환경변수 설정, Docker Compose, CI 및 28개 테스트를 갖추고 있었습니다.
실행 어댑터·전략·과거 데이터·DB 원장·PAPER·Slack은 없었습니다.

기존 조회 계약과 endpoint를 유지했습니다. 실제 HTTP 경계는 계속 조회 및 OAuth 토큰 발급만
허용합니다. 여기에 공식 분봉·호가·미국 캘린더 GET을 추가하고 독립된 PAPER 실행 경로를 구축했습니다.
실제 계좌 조회·실주문·Slack 발송·AWS 배포는 이번 작업에서 실행하지 않았습니다.

| 요구 영역 | 현재 결과 |
| --- | --- |
| 지정 종목만 거래 | SOXL·SOXS·TQQQ·SQQQ 기본 허용; 엔진/실행 어댑터 이중 검증 |
| 지표·진입·청산·리스크 | 같은 코드로 PAPER 및 백테스트에 적용 |
| 실시간 데이터 PAPER | 공식 REST 조회 poll runner 구현; 실제 키 연동 검증은 미실시 |
| 주문 상태·예약·재시작 | PAPER 원장 영속화, 부분 체결, 확정 취소/거부, UNKNOWN 및 중복 차단 |
| 실제 계좌와 상태 대조 | 읽기 전용 검증 함수 구현; 실주문 원장 자동 재구성·불일치 자동 복구는 미구현 |
| LIVE | 독립 어댑터 경계에서 활성화 거부; 실주문 전송 구현 없음 |
| 백테스트 | 실제 전략 재사용, CSV·비용·학습/검증·Buy & Hold·요청 성과 지표 |
| Slack | 트랜잭션 outbox 및 명시적 옵션 dispatcher, MockTransport 검증 |
| PostgreSQL | SQLAlchemy/psycopg URL 지원; 로컬 검증은 SQLite, PostgreSQL 서버 검증은 미실시 |
| Docker·CI | 기존 CI 유지, worker profile·공유 원장 volume·설정 mount 추가 |

## 2. 구현한 매매 규칙

### 진입

최신 완료 5분봉에서 종가>VWAP, EMA9>EMA21, 종가>EMA9, 거래량 비율≥1.5,
종가>직전 3개 5분봉 최고가를 모두 확인합니다. 거래량 평균은 현재 봉을 제외한 직전 20개입니다.
VWAP은 정규장 1분봉의 HLC3×거래량 누적값입니다. EMA는 SMA seed 후 지수 갱신,
ATR14는 true range를 Wilder 방식으로 평활합니다. 당일 지표를 초기화하고 최소 21개
5분봉이 있어야 하므로 기본 첫 진입 판단은 개장 105분 이후입니다.

기술적 후보에 대해 미보유·미체결 없음·정규장·개장 15분 이후·폐장 30분 이상 전,
손실 한도·종목 비중·슬롯·쿨다운·횟수·현금·최대 주문 금액·호가 신선도/스프레드/비용을 검사합니다.
데이터 누락, 미래 timestamp, 시세 지연, 미조정 UNKNOWN 주문이 있으면 진입하지 않습니다.
동시 신호는 TOML 종목 순서로 평가합니다. 임의 추천/검색/종목 추가 기능은 없습니다.

수량은 ATR×1.5 손절폭과 왕복 비용 추정을 포함한 0.5% 위험 예산, 남은 일일 손실 여유,
현금에서 미체결 예약액을 뺀 값, 20% 종목 투자금, 최대 주문 금액 중 가장 작은 제약으로
계산하고 최소 주문 단위로 내림합니다. 최소 단위 미만이면 주문하지 않습니다.

### 청산과 리스크

- 체결 평균가−1.5×진입 시 ATR에 손절, 체결 평균가+2×초기 위험폭에 전량 익절을 설정합니다.
  부분 매수 체결 시 평균가를 갱신하며 진입 시 ATR은 고정합니다.
- 손절·익절은 신선한 모의 호가의 bid로 감지하고 다음 호가 이벤트에서 실제 모의 체결합니다.
  손절 가격에 정확히 체결된다고 가정하지 않습니다.
- 완료 5분봉 종가가 VWAP과 EMA9 아래면 추세 청산합니다.
- 기본 폐장 15분 전부터 진입 주문의 미체결 잔량을 취소하고 청산을 시작합니다.
  5분 전에도 보유 상태면 FLATTEN_UNRESOLVED 경고를 저장합니다.
- 손절→익절→추세→마감 순으로 이유를 선택하며 같은 포지션에 청산 주문을 중복 제출하지 않습니다.
- 일일 시작 모의 평가액 대비 실현+미실현/비용 반영 손실이 1%에 도달하면 당일 신규 매수를
  계속 차단하고 기존 포지션 청산은 유지합니다. 가격이 회복돼도 당일 진입 제한을 해제하지 않습니다.
- 동시 보유/예약 종목 최대 3개, 종목별 일일 진입 최대 3회입니다.
  부분 체결을 새 진입으로 중복 계산하지 않습니다. 손절 체결 후 30분간 재진입을 차단합니다.
- 전일 미청산 포지션이 발견되면 경고하고 신규 매수를 막은 채 청산부터 시도합니다.

## 3. 주문·원장·알림 안전장치

`Execution`은 전략과 독립적입니다. PaperExecution은 HTTP client를 가지지 않고 모의 잔고만
갱신합니다. LiveTossExecution은 구성 시 중단됩니다. 설정의 LIVE와 기존 환경변수의
실거래 활성화 플래그만으로는 실제 주문이 가능해지지 않습니다.

주문은 SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, UNKNOWN 상태로 관리합니다.
체결 대기 또는 UNKNOWN 상태에서 같은 종목 주문을 추가하지 않습니다. 확정 REJECTED는
다음 위험 감지 이벤트에서 새 청산 시도를 허용하지만 UNKNOWN은 허용하지 않습니다.
모의 매수는 가격 상한을 두고, 동일 호가를 재사용한 중복 부분 체결을 차단합니다.

원장은 설정·현금·보유·미체결·체결/손익·횟수·쿨다운·당일 지표 입력을 저장합니다.
신호별 결정적 주문 ID와 revision 조건 갱신, 하나의 트랜잭션으로 상태/이벤트/outbox를 반영합니다.
재시작 시 모의 잔고의 무결성을 점검하고 미체결을 복원합니다. 설정이 달라지면 중단합니다.

`verify_external_account`는 실제 잔고·매수 가능 금액·미체결 목록 및 내부의 모든 미확정 주문
상세를 조회해 불일치를 반환합니다. PAPER의 로컬 ID를 실제 broker orderId로 간주해서는
안 됩니다. 이 함수는 향후 LIVE 원장용 검증 요소이며 실제 계좌 상태를 PAPER 원장에 덮어쓰지 않습니다.
LIVE의 실제 체결 이벤트 적용·취소 확인·멱등성·장애 복구는 추가 검증/구현이 필요합니다.

판단 이벤트에는 시간·종목·지표·거래량/평균·충족 조건·거부 이유·주문 ID·체결 상태·가격·비용·
손익·누적 일일 손익이 기록됩니다. Slack 이벤트는 체결, 손절, 일일 손실 한도, API 오류,
주문 실패, 미청산, 일일 결과를 포함합니다. 알림 실패가 주문 재실행을 유발하지 않습니다.
실제 webhook 전송은 `--slack`을 지정했을 때만 합니다. 전송 결과 불확실 상태는 운영자가 확인합니다.

## 4. 설정 및 변경 파일

`config/strategy.toml`의 전체 항목:

| 구분 | 파라미터 |
| --- | --- |
| 실행 | mode, strategy_name, symbols, initial_cash |
| 지표 | ema_fast, ema_slow, volume_period, volume_ratio, atr_period, breakout_bars |
| 손익/투자 한도 | stop_atr, reward_r, risk_fraction, daily_loss_fraction, symbol_fraction, max_order_usd |
| 보유/재진입 | max_positions, cooldown_minutes, max_entries |
| 시간 | entry_after_minutes, entry_before_close_minutes, no_buy_before_close_minutes, flatten_before_close_minutes, flatten_deadline_minutes |
| 데이터/비용 | max_quote_age_seconds, max_bar_age_seconds, max_spread_bps, max_cost_bps, fee_bps, slippage_bps |
| 체결/실행 | quantity_step, participation, paper_poll_seconds |

요청한 최대 위험/손실/비중/횟수 및 최소 쿨다운/시간에는 스키마 안전 경계를 두었습니다.
기본 Secret 환경변수 외에 STRATEGY_CONFIG_PATH, PAPER_DATABASE_URL, SLACK_WEBHOOK_URL을 추가했습니다.

신규 소스: trading_config.py, domain.py, strategy.py, risk.py, execution.py, engine.py,
persistence.py, marketdata.py, runner.py, reconciliation.py, download.py, backtest.py, notifications.py.
기존 수정: adapters/toss.py, api.py, config.py, pyproject.toml, uv.lock, Dockerfile, compose.yaml,
.env.example, tests/conftest.py, README.md, docs/architecture.md, docs/deployment.md.
추가 테스트: tests/test_trading.py, tests/test_trading_integration.py.
예제: examples/generate_synthetic.py. 기존 테스트 파일과 기존 CI 동작은 유지했습니다.

## 5. 검증과 백테스트 결과

로컬 pytest **92개 통과**. 기존 28개를 포함합니다. Ruff lint/format, wheel/sdist 빌드,
Compose 전체 profile 설정 검증도 통과했습니다.

검증 범위: 지표 값·거래량 분모·breakout 참조 범위, 누락/미완료/미래 데이터, 진입 시간 경계,
모든 리스크 제한, 최소 수량·비용, 부분 체결·중복·거부·UNKNOWN, 원장 재시작/동시 갱신,
손절/익절/추세/마감, 손실 후 청산, 캘린더 휴장/DST/조기 폐장, 실제 계좌 읽기 전용 대조,
분봉 cursor·오류 및 Slack 성공/타임아웃을 검증했습니다. 외부 TCP 통신은 차단했습니다.
Starlette의 HTTPX 지원 및 calendar 의존 라이브러리의 Pandas API 관련 경고 5건이 있습니다.

**다음은 합성 가격으로 실행한 엔진 검증 결과입니다. 실제 투자 성과가 아닙니다.**
선택한 4개 종목 각각 390분×4일, 총 6,240개 분봉을 생성했습니다.
학습: 2026-10-19~20 / 검증: 2026-10-21~22. 동일한 인위적 패턴을 반복했으므로
두 구간의 수치는 같으며 일반화·수익 가능성·과최적화 방지를 입증하지 않습니다.
초기 자금은 구간별 $10,000, 수수료 편도 2bp, 슬리피지 편도 3bp입니다.

| 지표 | 각 구간 |
| --- | ---: |
| 총수익률 | 0.451984% |
| MDD | 0.778552% |
| 승률 | 50% |
| 평균 손익비 | 2.106778 |
| Profit Factor | 2.106778 |
| 총 거래 수 | 12 |
| 거래당 평균 수익률 | 0.188642% |
| 수수료 차감 후 순수익 | $45.198397 |
| 합계 수수료 | $9.554762 |
| Buy & Hold 총수익률 | 0.330069% |
| 종료 시 미청산/미확정 | 0 / 0 |

종목별 순수익: SOXL $20.656757, SOXS $14.067486, TQQQ $10.474154, SQQQ $0.
동시 후보에서 설정 순서와 최대 3종목 제한 때문에 이 합성 예제의 SQQQ에는 진입하지 않았습니다.
종목별 수익률은 해당 종목 완료 거래의 순손익/진입 원가 합계로 정의합니다.
분할 매도 체결은 포지션별로 합산한 후 거래 수·승률 등을 계산합니다.
손실 거래가 없으면 Profit Factor/손익비는 null로 표시합니다.

재현 명령과 결과 JSON 경로:

```sh
uv run python examples/generate_synthetic.py
uv run python -m toss_trading.backtest data/synthetic.csv --split 2026-10-21 --output data/synthetic-report.json
```

실데이터 백테스트는 키/데이터가 제공되지 않아 미실시입니다. 입력 1분봉의 종료 timestamp와
분할/배당/수정주가를 검증한 뒤 별도 데이터로 수행해야 합니다. 실제 키 연결, PostgreSQL 서버,
Docker 이미지 실행 및 Lightsail 배포도 검증하지 않았습니다. 로컬 Docker 데몬은 실행되지 않았습니다.

## 6. 공식 API 지원 및 실거래 전 확인 사항

[공식 OpenAPI v1.2.24](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)를 확인했습니다.
조회는 `/api/v1/candles`의 interval=1m, `/api/v1/orderbook`,
`/api/v1/market-calendar/US`를 사용하며 가상 endpoint는 추가하지 않았습니다.
분봉 timestamp는 종료 시각입니다. 현재 공식 분봉은 1m/1d를 제공하므로 5분봉은 로컬 집계합니다.

공식 API에는 주문 생성/정정/취소 및 SINGLE/OCO/OTO 조건주문이 있습니다.
다만 미국 조건주문은 거래 가능한 모든 세션에서 발동할 수 있어 정규장 전용 보호 주문이라고
가정할 수 없습니다. 현재 client는 해당 POST를 차단합니다. 서버 측 보호 주문·실제 OCO 경합·
잔고와 체결을 대조한 보호 수량 변경은 구현/검증되지 않았습니다.
[실시간 WebSocket 명세](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json)도 있으나
이번 PAPER runner는 REST를 사용합니다. 체결 스트림 누락을 누적 거래량으로 오인하지 않습니다.

LIVE 이전에 다음을 확인해야 합니다.

1. 실제 키·IP·권한·통화·수수료·최소 수량·분봉 보존 범위·호가 timestamp/잔량을 검증합니다.
2. 실제 데이터로 독립 검증 기간을 충분히 확보하고 누락·거래정지·분할·급변·갭·거래 비용을 반영합니다.
3. 현재 분봉 내 open-low-high-close 모의 경로와 고정 스프레드는 실제 틱/호가 이력이 아닙니다.
   신호 다음 이벤트 체결을 쓰더라도 실제 지연·체결 확률·주문 큐를 검증해야 합니다.
4. 소프트웨어 손절과 0.5% 위험 예산은 갭/장애 시 실제 손실 상한을 보장하지 않습니다.
   REST 전체 순회 지연, 프로세스 중단, API/Slack 장애 중에도 보호할 방법이 필요합니다.
5. LIVE 원장의 broker ID/clientOrderId 매핑, 불확실 전송 후 체결 조회, 취소 확정, 재시작 시
   실제 잔고와 부분 체결 재구성, 주문/조건주문 경합을 검증해야 합니다. 불일치 시 신규 매수를 막아야 합니다.
6. 현재 client당 토큰 하나만 유효하므로 API·worker·다운로드의 같은 client 동시 사용을 피하거나
   단일 인증 소유권을 구현해야 합니다. 호출 한도는 응답 헤더와 함께 다시 검증합니다.
7. PostgreSQL 운영 마이그레이션/복구, DB 장애·동시성, Secrets 교체, 실거래 활성화 감사 기록을 검증합니다.

이 항목을 완료하기 전에는 LIVE 경계를 해제하지 않습니다. 기존 API와 PAPER 전략 개발은 독립적으로 계속할 수 있습니다.

## 7. 실데이터 재검증과 비교 PAPER (2026-10-25)

토스 공식 API 1분봉 54개 완전 세션(2026-07-24~10-23, 불완전 11개 제외)으로 기존 전략을 재생했습니다.
학습 7/24~9/30(43세션), 검증 10/1~10/23(11세션)입니다. 체결은 모형화된 분봉 내 경로이며 실제 체결이 아닙니다.

| 전략 | 학습 순손익 / PF | 검증 순손익 / PF |
| --- | ---: | ---: |
| vwap_breakout (기본값) | −$432.99 / 0.51 | +$23.02 / 1.08 |
| vwap_mean_revert (k=1.5, 손절 2 ATR, 3R) | +$935.52 / 1.82 | −$184.75 / 0.65 |

- 기존 전략의 손실 대부분은 손절(140건 중 69건, −$878)이었으나 손절폭 확대·추세 청산 제거·진입 강화는
  학습 구간에서 모두 PF 0.40~0.57로 개선되지 않았습니다.
- 학습 구간에서 BUY_SIGNAL 이후 장 마감까지 평균 수익률은 −80bp였습니다. 7~9월은 VWAP 이탈 후 회귀가 우세했습니다.
- 평균회귀 후보는 검증 전에 선택했고 학습 구간에서 k=1~2, 손절 2~3 ATR 모두 수익이었지만 검증 구간에서는 모두 손실이었습니다.
  월별로 평균회귀는 7~9월 각 +$288~+$317, 10월 −$168이며 기존 전략은 그 반대였습니다.
- 장세 전환이 한 번뿐이므로 장세 필터를 이 데이터로 맞추지 않았습니다. 10월 데이터는 검증에 사용됐으므로 재튜닝에 쓰지 않습니다.

사용자 결정에 따라 `vwap_breakout`을 제거하고 `vwap_mean_revert`를 유일한 전략으로 전환했습니다.
지표 계산·워밍업은 그대로 두어 위 백테스트 수치를 저장소 코드로 동일하게 재현함을 확인했습니다.
추세 청산은 VWAP 회복 청산(`VWAP_EXIT`)으로 바뀌었고 `volume_ratio`, `breakout_bars` 설정은 삭제되었습니다.
2절의 진입·추세 청산 규칙은 이전 전략 기준이며 리스크·주문·원장 규칙은 그대로 적용됩니다.
10월 검증 구간에서 손실이었으므로 2026-10-26 이후 PAPER 기록을 독립 표본으로 삼아 계속 검증합니다.
설정 항목이 바뀌어 기존 PAPER 원장은 새로 시작해야 합니다.

## 8. 구조 개선과 검증 도구 (2026-10-26)

요청서는 이전 breakout 전략 기준이었으므로 현재 `vwap_mean_revert`에 맞게 다음을 조정했습니다.
수익률을 높이기 위한 파라미터 변경은 하지 않았고, 합성 데이터 결과로 기본값을 정하지 않았습니다.
LIVE 경계·HTTP 주문 차단·실주문 코드는 변경하지 않았습니다.

- 시간대 거래량(1-3): 현재 전략은 거래량을 진입에 쓰지 않으므로 비율 계산만 바꾸고,
  진입 필터 `min_volume_ratio`를 새로 두되 기본은 끔. "이력 부족 시 진입 안 함"은 필터를 켤 때 적용.
- 민감도(2-3): `ema_fast/ema_slow`, `volume_ratio`, `breakout_bars`는 현재 판단에 영향이 없거나
  삭제된 항목이어서 `stretch_atr`, `stop_atr`, `reward_r`, `atr_period`(+켜진 RSI·거래량 필터)를 사용.
- `vwap_only`(2-2): 현재 전략의 VWAP 회복 청산을 그대로 쓰면 진입 직후 청산되므로 공통 청산
  (손절·익절·마감)은 같게 두고 고유 청산은 "종가 < VWAP"으로 대칭 처리.
- RSI: 사용자 요청으로 `rsi_entry_max` 필터를 추가(기본 끔). RSI14 ≤ 30 변형은 사전 고정값이며
  `config/strategy-rsi.toml`로 비교 PAPER에서 검증.

### 8.1 변경 파일

| 파일 | 변경 |
| --- | --- |
| `trading_config.py` | 그룹·워밍업·거래량·RSI·비용 설정과 검증, 비용 합산 helper |
| `strategy.py` | 공통 지표 클래스, 전일 seed·폴백 사유, 시간대/rolling 거래량, Wilder RSI, `vwap_only`·`random_entry` 기준 전략 |
| `risk.py` | 반대 그룹 차단, 그룹 위험 예산(부분 체결 이중 계산 방지), 새 비용의 비용 필터·위험 예산 반영, 거부 코드 |
| `execution.py` | 매수 환전비, 매도 SEC·TAF, 체결별 슬리피지 기록 |
| `engine.py` | 세션 종료 시 5분봉·슬롯 거래량 보관(조기 폐장 제외), 전일 확인, WARMUP_FALLBACK·VOLUME_HISTORY_INSUFFICIENT·거부 사유 기록, 전략 주입. 날짜 전환 시 이전 세션을 덮어쓰던 순서를 수정 |
| `domain.py` | `prior_day`, `prior_bars`, `slot_volumes`, `Position.entry_slippage` |
| `marketdata.py` | `previous_session` (휴장 건너뜀) |
| `runner.py` | 새 원장에서 전일 정규장 분봉을 하루 한 번 조회해 seed, 실패 시 폴백 |
| `backtest.py` | 메모리 원장, `--baselines`·`--seeds`·`--sensitivity`·`--walk-forward`·`--workers`, 리포트 보강 |
| `dataload.py` (신규) | 벤더 중립 CSV 검증·변환 CLI |
| `config/strategy*.toml` | 새 기본값, 기존 동작 재현(`legacy`), RSI 변형 |
| 테스트 | `conftest.py`(기존 테스트를 legacy 설정에 고정), `test_structure.py`·`test_validation_tools.py` 신규, 통합 테스트 1개 추가 |

기존 테스트의 기대값은 바꾸지 않았습니다. `config` fixture가 `strategy-legacy.toml`을 읽도록 했고,
API 테스트 1건은 원장과 같은 설정 파일을 쓰도록 준비 단계만 바꿨습니다(새 기본값과 원장 설정 불일치 방지).

### 8.2 설정 항목

README "구조 옵션" 표 참조. 기존 동작은 `config/strategy-legacy.toml`로 재현되며, 실데이터
재생에서 legacy 설정이 7절 수치(학습 +$935.52/PF 1.82/172건, 검증 −$184.75/PF 0.65/43건)를
새 백테스트 모듈과 새 CSV 로더를 거쳐 동일하게 재현함을 확인했습니다.

### 8.3 테스트

pytest **126개 통과**(기존 92 + 신규 34), Ruff lint/format 통과, 경고 5건(기존 Starlette/Pandas).
그룹(반대 그룹·예약 포함, 예산 소진, 부분 체결, 설정 검증, 거부 기록), 워밍업(정시 진입, 휴장 직후,
DST 전환, 조기 폐장 다음날, 누락일, 갭 있는 전일, 분할 의심, runner seed·실패 폴백),
시간대 거래량(이력 부족 block/rolling, 조기 폐장 제외, lookback 상한), 비용(필터·위험 예산·
SEC/TAF 상한·legacy 공식), RSI 값과 on/off, random_entry 시드 재현성, 기준선·민감도·walk-forward·
리포트 형식, 로더 검증 항목을 다룹니다.

### 8.4 합성 데이터 실행 (엔진 동작 확인용)

**합성 가격은 시장 이력이 아니며 아래 수치로 성과를 해석하면 안 됩니다.**
`data/synthetic.csv`(4일·4종목), 기본 설정, 20 seeds:
current −2.11%(16건), vwap_only 거래 없음, random_entry 중앙값 −0.88%(목표 8회 진입),
Buy & Hold +0.13%. 각 경로가 실행되고 리포트가 생성되는지만 확인한 결과입니다.

### 8.5 수집된 실데이터 실행 (참고)

7절의 54개 완전 세션을 `dataload --drop-incomplete`로 변환해 **새 기본값**으로 실행했습니다.
이 기간은 이미 전략 선택에 쓰였으므로 독립 검증이 아닙니다.

| 실행 | 수익률 | PF | 승률 | 거래 | MDD |
| --- | ---: | ---: | ---: | ---: | ---: |
| current (전체) | −1.55% | 0.94 | 52.5% | 276 | 4.3% |
| vwap_only | −16.46% | 0.27 | 14.2% | 275 | 17.3% |
| random_entry 20 seeds | 중앙값 −6.31% (−9.25% ~ −2.59%) | 0.37~0.79 | | 약 170~190 | |
| Buy & Hold | −10.53% | | | | |

- current는 random_entry 20개 모두보다 수익률·PF가 높았습니다(백분위 100). 다만 random 진입이
  VWAP 위에서 일어나면 현재 청산 규칙상 곧바로 청산되어 비용만 지불하고, 실제 random 거래 수가
  목표(258)보다 적습니다. 진입 신호의 우위를 시사하지만 이 구조적 불리함을 감안해야 합니다.
- 학습(7~9월) +0.56%/PF 1.03, 검증(10월) −2.55%/PF 0.58. 학습 구간 비용 차감 전 +$1,075, 비용 $1,019.
  legacy 비용(2bp/3bp)에서 +$935였던 결과가 임시 비용값(10bp/5bp)에서 거의 0이 되었습니다.
  **실제 수수료 입력 전에는 순손익을 판단할 수 없습니다.**
- 민감도: 16개 이웃 모두 PF 0.84~0.99(기준 0.94)로 성과가 안정적이지만 모두 1 미만입니다.
- Walk-forward(20/5, 7구간): 양수 4구간, 구간 수익률 중앙값 +0.40%, 평균 −0.21%. 10월 두 구간이 손실.

### 8.6 실데이터가 들어오면 실행할 순서

```sh
# 1) 변환·검증 (리포트의 누락·분할 의심·정규장 외 건수 확인)
uv run python -m toss_trading.dataload raw.csv --timestamp-at end --drop-incomplete --output data/new.csv
# 2) config/strategy.toml에 실제 fee_bps / fx_spread_bps / sec_fee_bps / taf_* 입력
# 3) 엣지: random 대비 백분위와 vwap_only 비교
uv run python -m toss_trading.backtest data/new.csv --baselines --seeds 50 --output data/baselines.json
# 4) 안정성: 이웃 조합의 분산 확인 (최적값 선택 금지)
uv run python -m toss_trading.backtest data/new.csv --sensitivity --output data/sensitivity.json
# 5) 일관성: 고정 파라미터 구간별 성과
uv run python -m toss_trading.backtest data/new.csv --walk-forward 20,5 --output data/wf.json
# 6) RSI 변형도 같은 순서로 (--config config/strategy-rsi.toml)
```

### 8.7 미구현·판단 필요 사항

1. **실제 비용 값**: 토스증권 해외주식 수수료·환전 우대율, 현행 SEC fee·TAF 요율을 입력해야 합니다.
   SEC/TAF 기본값은 확정 요율을 임의로 넣지 않으려고 0으로 두었습니다.
2. **분할 판단**: 기업행위 데이터가 없어 전일 대비 40% 초과 갭을 분할 의심으로 간주합니다.
   3배 레버리지 ETF의 실제 급등락과 구분하지 못할 수 있습니다.
3. **시간대 거래량 이력**: PAPER 새 원장은 20거래일 동안 이력이 없습니다. 과거 분봉으로 미리
   채우는 기능은 만들지 않았습니다(필터가 꺼진 기본값에서는 영향 없음).
4. **random_entry 설계**: 현재 청산 규칙과 결합하면 VWAP 위 무작위 진입이 즉시 청산됩니다.
   "진입 시점 VWAP 아래인 경우로 제한한 무작위" 같은 대안 기준선을 추가할지 결정이 필요합니다.
5. **Walk-forward 학습 구간**: 이번 단계는 파라미터를 고르지 않으므로 학습 구간은 이력 제공용입니다.
6. **기존 PAPER 원장**: 설정 JSON이 바뀌어 기존 원장은 새로 시작해야 합니다.
7. **SQLite 재생**: 오프라인 재생을 메모리 원장으로 바꿨습니다. 직렬화 경로는 PAPER와 테스트가 검증합니다.
