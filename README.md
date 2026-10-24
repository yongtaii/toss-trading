# Toss Trading

사용자가 지정한 **SOXL, SOXS, TQQQ, SQQQ**의 미국 정규장 단타 전략과 PAPER 실행기입니다.
기존 토스증권 공식 REST 조회 API를 유지하고 전략·리스크·모의 체결·원장·백테스트를 추가했습니다.
실주문 전송은 구현·활성화하지 않았습니다. `LIVE_TRADING_ENABLED=true`, `mode="LIVE"` 모두
실행이 거부됩니다. LLM은 매매 의사결정 및 실행 경로에 포함되지 않습니다.

전체 요구사항 대조, 변경 파일, 검증 결과, API 지원 범위와 남은 위험은
[구현 보고서](docs/strategy-report.md)에 있습니다.

## 설치와 기존 조회 API

Python 3.12+와 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```sh
uv sync --frozen --python 3.12
cp .env.example .env
uv run uvicorn toss_trading.api:create_app --factory --host 127.0.0.1 --port 8000
```

`.env`에 토스증권에서 발급한 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`와 32자 이상의 임의 값
`OPERATIONS_API_KEY`를 입력합니다. 공식 WTS Open API 설정에서 실행 환경의 공인 IP를
허용 목록에 등록해야 합니다. 키와 webhook URL은 Git에 넣지 마세요.

로컬 문서: <http://127.0.0.1:8000/docs>. 각 조회 요청에 `x-api-key`를 입력합니다.
`GET /v1/accounts` 응답의 `accountSeq`를 `.env`의 `TOSS_ACCOUNT_SEQ`에 설정한 뒤
재시작하면 계좌 기반 조회가 가능합니다. 계좌번호 `accountNo`와 다릅니다.
시장 데이터 및 PAPER 실행에는 실제 계좌 식별 값이 필요하지 않습니다.

| 운영 API | 내용 |
| --- | --- |
| `GET /health` | 프로세스 생존 상태; 증권사 연결 상태는 아님 |
| `GET /v1/prices?symbols=SOXL,SOXS,TQQQ,SQQQ` | 현재가·통화·데이터 시각 |
| `GET /v1/accounts` | 실제 계좌 목록 |
| `GET /v1/holdings?symbol=SOXL` | 실제 보유 자산; symbol 생략 시 국내·미국 전체 |
| `GET /v1/buying-power` | 실제 USD 현금 매수 가능 금액; 전체 예수금과 다름 |
| `GET /v1/orders?status=OPEN` | 실제 진행 주문 전체 |
| `GET /v1/orders?status=CLOSED&limit=20` | 실제 종료 주문 페이지 |
| `GET /v1/orders/{order_id}` | 실제 주문 상태 재조회 |
| `GET /v1/paper` | 별도 모의 원장·주문·포지션 |
| `GET /v1/paper/events?limit=50` | 최근 모의 판단·체결 기록 |

실제 조회 응답은 공식 `result` 구조와 금액 문자열을 보존합니다. 주문 목록은 `symbol`,
`from`, `to`, `cursor`, `limit`을 지원합니다. 날짜 필터는 KST 기준이며 `nextCursor`를
다음 요청의 `cursor`로 전달합니다. 기존 조회 API는 선택 종목 외의 정보도 조회할 수 있으나,
전략과 주문 실행 경계는 설정된 거래 허용 목록만 받습니다.

## PAPER 대시보드

프론트엔드 빌드나 Node 설치 없이 기존 FastAPI가 `/dashboard/`를 제공합니다.
`.env`에 `DASHBOARD_USERNAME=admin`과 별도의 무작위 `DASHBOARD_PASSWORD`(16자 이상)를
설정하고 API를 시작하세요. 브라우저 로그인에는 운영 API 키가 필요하지 않습니다.
비밀번호가 없으면 대시보드는 비활성화됩니다. 외부 접근에는 HTTPS가 필요합니다.

```sh
uv run uvicorn toss_trading.api:create_app --factory --host 127.0.0.1 --port 8000
# 브라우저: http://127.0.0.1:8000/dashboard/
# 다른 터미널에서 실제 시세 수집과 PAPER 운용
uv run python -m toss_trading.runner
```

- 총자산·현금·예약금, 실현/미실현 손익, 누적 수수료
- 매도 체결별 누적 실현손익 그래프, 포지션·미체결 주문, 최근 100개 이벤트 필터
- 기본/비교 원장 전환, 종목별 평가 시각, 엔진 마지막 기록 및 일일 손실 한도 상태
- 종목별 마지막 5분봉 종가·VWAP·ATR·진입 기준과 조건별 통과/차단 결과
- 독립적인 수집기 heartbeat, 시세/시장 일정 마지막 수집 성공 및 최근 수집 문제
- 다음 개장/현재 정규장 마감 카운트다운(한국 시간 표시)
- 10초 자동 갱신, 모바일 레이아웃, 연결 실패/갱신 지연 표시

대시보드는 `/dashboard/data`에서 원장만 읽으며 증권사 요청이나 주문을 보내지 않습니다.
총자산은 기존 `/v1/paper`와 같이 마지막 평가가 기준의 매도 비용 차감 전 금액입니다.
미실현손익에는 매수 비용을 반영하며, 평가가가 없는 포지션의 요약 평가는 진입가를 사용합니다.
그래프는 자산 곡선이 아니라 비용 반영 후 누적 **실현손익**입니다. 부분 매도도 체결 건수에 포함됩니다.
기존 엔진 갱신 표시는 `last_event` 기준이며, 별도의 수집기 상태는 5초 주기 heartbeat로 확인합니다.
시세 수집 성공은 모든 대상 종목의 신선한 분봉·호가를 받은 경우에만 기록합니다. 장외 조회는
시장 일정 성공만 갱신하며 시세 성공으로 집계하지 않습니다. 이전 성공·오류 시각은 복구 후에도 보존합니다.
heartbeat는 최근 `max(30초, poll 주기×3)` 내 기록이면 실행 중, 정상 종료는 종료됨으로 표시합니다.
진입 조건은 엔진이 실제 기록한 5분봉 판단과 공통 리스크 로직의 현재 원장 조건을 함께 표시하며,
실시간 호가 비용·수량 검증을 통과한 주문을 보장하지 않습니다. 5분봉 판단이 5분+분봉 유효 시간보다
오래되면 이전 기록으로 표시합니다. 현재 거래일 증권사 일정이 없으면 로컬 거래소 캘린더의
예상 시간을 명시합니다. 카운트다운은 매매 실행 경로에 사용하지 않습니다.

기존 worker는 재시작해야 새 모니터링 기록을 남깁니다. 최초 재시작 시 `paper_worker_status`와
`paper_symbol_signals` 테이블을 추가하며 거래 원장 JSON과 설정은 변경하지 않습니다. 이전 DB도
대시보드에서 읽을 수 있고 새 기록이 없으면 미확인으로 표시합니다. 최신 판단은 종목별로 별도 보관해
최근 100개 이벤트 범위 밖으로 밀려나도 유지합니다.
원장이 없거나 설정이 다르면 오류를 표시하고 기존 원장의 스키마·설정을 변경하지 않습니다.

## 전략 설정

전략은 `vwap_mean_revert` 하나입니다. 완료된 5분봉 종가가 당일 VWAP보다
`stretch_atr`(기본 1.5)×ATR14 이상 아래에 있으면 매수하고, 종가가 VWAP을 회복하면 청산합니다(VWAP_EXIT).
손절은 체결가−2×ATR, 익절은 3R입니다. 7~9월 실데이터 재생에서는 수익이었으나 10월 검증 구간에서는
손실이었으므로 PAPER로 전진 검증 중입니다([보고서 7절](docs/strategy-report.md)).
이전 `vwap_breakout`은 같은 기간 PF 0.64로 손실이어서 제거했습니다.

[config/strategy.toml](config/strategy.toml)에서 종목, 지표 기간, 이탈 폭,
손절·익절, 비용, 데이터 유효 시간, 주문 금액, 장 마감 시간 등을 변경합니다.
위험 비율·종목 비중·최대 포지션·진입 횟수·쿨다운에는 요청한 안전 한도를 적용합니다.
동시 신호는 설정의 종목 나열 순서로 평가하며 최대 3개만 주문 예약합니다.

지표는 당일 정규장 1분봉을 모아 완료된 5분봉에서 계산합니다.
VWAP은 1분봉 HLC3×거래량의 누적값으로 근사하며 **항상 당일 정규장 기준으로 리셋**합니다.
ATR·RSI는 Wilder 방식입니다. EMA9/21과 거래량 비율은 판단 기록용입니다.
정규장 시작 이후 누락된 분봉을 채우지 못하면 그 종목의 신규 진입을 중단합니다.

### 구조 옵션 (2026-10-26 추가)

| 설정 | 기본값 | 기존 동작 재현 값 | 내용 |
| --- | --- | --- | --- |
| `groups` | LONG: SOXL·TQQQ / SHORT: SOXS·SQQQ | `[groups]` 비움 | 상관 종목 방향 그룹 |
| `opposing_groups` | `[["NASDAQ_LONG","NASDAQ_SHORT"]]` | `[]` | 서로 반대 방향인 그룹 쌍 |
| `allow_opposing_groups` | `false` | `true` | 반대 그룹 보유·예약 중 신규 진입 차단(OPPOSING_GROUP) |
| `max_group_risk_fraction` | `0.005` | (그룹 없음) | 그룹 합산 위험 한도(GROUP_RISK_LIMIT) |
| `indicator_warmup` | `prior_session` | `intraday_only` | EMA·ATR·RSI를 전일 정규장 5분봉으로 seed |
| `split_gap_fraction` | `0.4` | - | 전일 종가 대비 갭이 이보다 크면 분할 의심으로 seed 생략 |
| `volume_mode` | `time_of_day` | `rolling` | 같은 5분 슬롯의 과거 평균 대비 거래량 |
| `volume_lookback_days` | `20` | - | 슬롯 평균에 쓰는 과거 정규장(조기 폐장일 제외) |
| `volume_fallback` | `block` | - | 이력 부족 시 거래량 조건 실패 또는 `rolling` 폴백 |
| `min_volume_ratio` | 없음(끔) | 없음 | 거래량 진입 필터. 켤 때만 위 두 항목이 진입에 영향 |
| `rsi_period` / `rsi_entry_max` | `14` / 없음(끔) | 없음 | RSI 과매도 진입 필터 |
| `fee_bps` / `slippage_bps` | `10` / `5` | `2` / `3` | 편도 수수료·슬리피지(보수적 임시값) |
| `fx_spread_bps` | `0` | `0` | 편도 환전 비용 |
| `sec_fee_bps` / `taf_per_share` / `taf_max_usd` | `0` / `0` / `0` | `0` | 매도 시 SEC fee(bp)·FINRA TAF(주당, 상한) |

전체 재현 설정은 [config/strategy-legacy.toml](config/strategy-legacy.toml)입니다.
[config/strategy-rsi.toml](config/strategy-rsi.toml)은 기본값에 RSI14 ≤ 30만 더한 비교용 변형입니다.

> **비용 값은 확정값이 아닙니다.** `fee_bps=10`, `slippage_bps=5`는 보수적 임시값이고
> 환전·SEC fee·TAF는 0입니다. 실제 토스증권 해외주식 수수료·환전 우대 조건과
> 현행 SEC/FINRA 요율을 직접 확인해 입력하세요. 실데이터 재생에서 비용이 손익을 좌우했습니다.

전일 seed를 쓰면 개장 15분(`entry_after_minutes`) 후부터 진입 판단이 가능합니다.
전일 데이터가 없거나(첫 실행·데이터 누락) 짧거나 분할이 의심되면 당일 데이터만 쓰고
`WARMUP_FALLBACK` 이벤트를 남기며, 이때는 개장 후 최소 105분이 필요합니다.
PAPER runner는 원장에 전일 데이터가 없으면 전일 정규장 분봉을 하루 한 번 조회합니다.
시간대 거래량은 원장에 쌓인 과거 정규장으로 계산하므로 새 원장에서는 20거래일 동안
`VOLUME_HISTORY_INSUFFICIENT`가 기록됩니다(거래량 필터를 끈 기본값에서는 진입에 영향 없음).

## 실제 시세 기반 PAPER

```sh
uv run python -m toss_trading.runner --once
uv run python -m toss_trading.runner
```

PAPER의 시작 자금은 설정의 `initial_cash`이며, 실제 계좌 현금·보유 종목과 독립적입니다.
공식 시장 캘린더, 1분봉, 호가를 REST로 조회하고 로컬에서만 체결을 시뮬레이션합니다.
정규장 밖에서는 체결하지 않습니다. 휴장·조기 폐장·서머타임은 공식 캘린더로 처리합니다.

기본 원장은 `data/paper.db`입니다. PostgreSQL을 사용하려면 `.env`에 다음 형태로 설정합니다.

```dotenv
PAPER_DATABASE_URL=postgresql+psycopg://trading:YOUR_SECRET@localhost:5432/trading
```

실제 주소·암호를 입력하고, 저장소에 커밋하지 마세요. 스키마는 최초 실행 때 생성합니다.
원장 복구 시 설정이 달라지면 중단합니다. 설정을 바꿀 때는 미체결·보유 상태를 확인한 후
새 원장으로 시작하거나 검토된 데이터 이전을 수행해야 합니다. 운영 DB의 버전별
마이그레이션과 백업 복원 검증은 별도 운영 작업입니다.

Slack 전송은 `.env`의 `SLACK_WEBHOOK_URL`을 설정하고 아래 옵션을 명시해야 활성화됩니다.

```sh
uv run python -m toss_trading.runner --slack
```

체결, 손절, 일일 손실 한도, API 오류, 청산 실패, 일일 결과는 outbox에 저장합니다.
전송 타임아웃은 UNKNOWN으로 남겨 중복 전송을 피하며, FAILED/UNKNOWN/SENDING의 운영자
확인·재처리가 필요합니다. 테스트·백테스트는 Slack을 호출하지 않습니다.

### 설정 비교 PAPER (선택)

다른 설정 파일(예: `stretch_atr`만 바꾼 사본)을 같은 시세로 동시에 모의 운용할 수 있습니다.
같은 client로 프로세스를 둘 띄우면 토큰이 서로를 무효화하므로, 하나의 runner가 시세를 한 번
조회해 두 설정에 공급하고 원장만 분리합니다.

```dotenv
COMPARE_CONFIG_PATH=config/strategy-variant.toml
COMPARE_DATABASE_URL=sqlite:///data/paper-variant.db
```

두 값은 함께 설정해야 하며 비교 원장은 기본 원장과 달라야 합니다. 상태는
`GET /v1/paper?ledger=compare`, `GET /v1/paper/events?ledger=compare`로 조회합니다.

## 과거 데이터와 백테스트

공식 API 접근 권한이 있으면 선택 종목의 분봉을 내려받을 수 있습니다. 실제 제공 범위·보존 기간은
발급된 계정으로 확인해야 합니다. 다음 명령은 조회만 하며 주문을 제출하지 않습니다.

```sh
uv run python -m toss_trading.download --start 2026-10-19 --end 2026-10-23 --output data/history.csv
uv run python -m toss_trading.backtest data/history.csv --split 2026-10-22 --output data/backtest.json
```

CSV 열: `symbol,end,open,high,low,close,volume`. `end`는 타임존이 있는 **1분봉 종료 시각**입니다.

### 외부 1분봉 CSV 검증·변환

특정 벤더 API 연동 없이 `timestamp,open,high,low,close,volume`(+`symbol` 열 또는 `--symbol`)을
받아 검증하고 위 형식으로 변환합니다. 오프셋이 없는 timestamp는 `--timezone`으로 해석합니다.

```sh
uv run python -m toss_trading.dataload vendor.csv --symbol SOXL --timestamp-at start \
  --timezone America/New_York --drop-incomplete --output data/soxl.csv
```

리포트(`<output>.report.json`): 잘못된 행, 중복(값 충돌 포함), 역순, 정규장 외 분리 건수,
세션별 누락 분, 조기 폐장일, 전일 종가 대비 40% 초과 갭(분할 의심) 경고.

### 엣지 검증 도구

```sh
# 기준선: current / vwap_only / random_entry(20 seeds) / buy_and_hold, random 대비 백분위
uv run python -m toss_trading.backtest data/history.csv --baselines --seeds 20
# 파라미터 민감도: 주요 결정 파라미터를 ±12.5%, ±25% 하나씩 변경 (최적값 추천 없음)
uv run python -m toss_trading.backtest data/history.csv --sensitivity
# Walk-forward: 학습 20일/검증 5일 롤링, 파라미터 고정
uv run python -m toss_trading.backtest data/history.csv --walk-forward 20,5
```

옵션은 함께 쓸 수 있으며 결과는 `--output` JSON에 모입니다. `random_entry`는 현재 전략의
진입 시각 분포와 횟수를 유지한 채 날짜·종목만 무작위로 바꾸고, 청산·리스크·비용은 동일합니다.
리포트에는 30분 단위 진입 시간대별 성과, 종목·그룹별 성과, 비용 차감 전/후 손익,
거래 100건 미만 시 표본 부족 경고가 포함됩니다.
현재 구현은 VWAP과 분봉 내 위험 처리를 위해 1분봉을 요구합니다. 5분봉을 1분봉으로
임의 보간하지 않습니다. 수정주가 여부·거래량 조정·분할·배당은 입력 데이터에서 검증하세요.

같은 전략·리스크·체결 엔진을 시간 순서로 재생합니다. 신호 봉 종가에 즉시 체결하지 않고
다음 모의 호가 이벤트에서 체결합니다. 수수료·슬리피지·부분 체결·현금 예약을 반영합니다.
분봉 내 호가는 open→low→high→close 경로와 1bp 스프레드로 근사하며,
체결 잔량은 이전 완료 분봉 거래량에서 추정합니다. 실제 체결 가능성을 입증하지 않습니다.

`--split`은 미국 현지 거래일 기준이며 학습/검증 기간을 별도 원장으로 실행합니다.
자동 파라미터 탐색은 하지 않습니다. 검증 구간 첫날은 전일 seed 없이 시작합니다.
오프라인 재생은 메모리 원장을 씁니다(SQLite 재생과 체결·지표가 동일함을 확인).
보고서에 수익률·MDD·승률·손익비·Profit Factor·거래 수·평균 거래 수익률·순손익·비용·
종목별 결과와 동일 기간/비용의 균등 자금 Buy & Hold 비교가 저장됩니다.

네트워크 없이 전체 실행 경로를 확인하는 **합성 데이터 예제**:

```sh
uv run python examples/generate_synthetic.py
uv run python -m toss_trading.backtest data/synthetic.csv --split 2026-10-21 --output data/synthetic-report.json
```

합성 데이터 결과는 전략의 투자 성과가 아닙니다. 실데이터 백테스트는 아직 수행하지 않았습니다.

## 검증·컨테이너

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

테스트는 HTTP MockTransport와 pytest-socket으로 실제 증권사/Slack TCP 통신을 차단합니다.
CI에는 증권사 자격증명을 주입하지 않습니다. 토큰 발급 이외의 POST와 모든 실제 주문 변경은
HTTP 전송 전에 차단합니다. 수동 조회 점검은 기존 명령을 유지합니다.

```sh
uv run python -m toss_trading.smoke
uv run python -m toss_trading.smoke --include-account
```

Docker에서는 기본 API와 선택적 PAPER worker를 실행할 수 있습니다.

```sh
docker compose up --build -d
docker compose --profile paper up --build -d
# PostgreSQL 사용 시 POSTGRES_PASSWORD와 PAPER_DATABASE_URL을 먼저 설정
# Compose 내부 PostgreSQL 주소는 localhost가 아니라 postgres:5432
docker compose --profile storage --profile paper up --build -d
```

API는 127.0.0.1:8000에만 열립니다. PAPER 원장은 named volume에 저장합니다.
기본 Compose는 증권사 키를 PAPER worker에만 전달합니다. API는 원장 기반 대시보드를 제공합니다.
컨테이너의 실제 계좌/시세 조회 API도 사용하려면 별도 client의 자격증명을 API에 주입하세요.
같은 client를 API와 worker에서 동시에 사용하면 토큰이 서로 무효화될 수 있습니다.
[배포 계획](docs/deployment.md)을 참고하세요. 로컬 Docker 데몬은 실행되지 않아 기동 검증은 미실시입니다.

## 공식 명세와 한계

- [공식 REST OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json): v1.2.24 확인
- [공식 개요·호출 한도](https://openapi.tossinvest.com/openapi-docs/overview.md)
- [공식 실시간 AsyncAPI](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json)

현재 client당 유효 토큰은 하나입니다. 조회 client는 토큰을 메모리에 보관하고 전체 요청을
1.05초 이상 간격으로 직렬화합니다. 401/403/429/5xx를 무조건 재시도하지 않고 다음 poll에서
재조회하며 신규 진입을 중단합니다. API 조회 실패 시 운영 API는 upstream_status와 함께 502를
반환합니다. 수동 smoke/download도 같은 client의 운영 worker와 동시에 실행하지 마세요.

REST 폴링은 전체 순회 시간만큼 지연됩니다. `paper_poll_seconds`는 순회 후 대기 시간이며
틱 수준의 손절 응답시간을 보장하지 않습니다. WebSocket 기반 체결 처리와 공유 토큰 소유권,
실제 주문 상태/잔고 복원 및 보호 주문 검증을 마치기 전에는 LIVE를 사용할 수 없습니다.
