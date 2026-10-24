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
VWAP은 1분봉 HLC3×거래량의 누적값으로 근사합니다. ATR은 Wilder 방식입니다.
EMA9/21과 거래량 비율은 판단 기록용이며 백테스트와 같은 워밍업을 유지합니다.
지표를 매일 초기화하므로 기본값에서 첫 진입 판단에는 최소 **105분의 연속 데이터**가 필요합니다.
정규장 시작 이후 누락된 분봉을 채우지 못하면 그 종목의 신규 진입을 중단합니다.

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
현재 구현은 VWAP과 분봉 내 위험 처리를 위해 1분봉을 요구합니다. 5분봉을 1분봉으로
임의 보간하지 않습니다. 수정주가 여부·거래량 조정·분할·배당은 입력 데이터에서 검증하세요.

같은 전략·리스크·체결 엔진을 시간 순서로 재생합니다. 신호 봉 종가에 즉시 체결하지 않고
다음 모의 호가 이벤트에서 체결합니다. 수수료·슬리피지·부분 체결·현금 예약을 반영합니다.
분봉 내 호가는 open→low→high→close 경로와 1bp 스프레드로 근사하며,
체결 잔량은 이전 완료 분봉 거래량에서 추정합니다. 실제 체결 가능성을 입증하지 않습니다.

`--split`은 미국 현지 거래일 기준이며 학습/검증 기간을 별도 원장으로 실행합니다.
자동 파라미터 탐색은 하지 않습니다. 지표 워밍업도 각 세션에서 새로 시작합니다.
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
API와 worker는 client를 공유하므로 토큰 충돌을 피하려면 증권사 키를 API에 주입하지 않고
PAPER worker만 시장 데이터 조회에 사용하거나, 서비스별로 별도 발급한 client를 사용해야 합니다.
기본 compose는 편의상 같은 키를 전달하므로 실제 조회 API와 worker를 동시에 호출하지 마세요.
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
