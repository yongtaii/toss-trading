# Toss Trading

일봉 SMA20/200을 주 첫 거래일에 판단하는 ETF 전략의 오프라인 비교 프로젝트입니다.
기본 전략과 목표 투자 비중 90·95·99·100%의 **다섯 전략만 유지**합니다.
토스증권 조회 API·시세 다운로드·공통 원장과 주문 처리 기반 코드는 함께 보존합니다.

## 남긴 전략

기본값은 **기본 전략**(이전 이름: 기존 소액 운용, 내부 ID: `legacy_weekly`)입니다.
90·95·99·100%형은 비교 후보로 유지합니다.

| 이름 | 자금 배분 | 연구 명세 |
| --- | --- | --- |
| 기본 전략 | 위험 예산 0.5%, 진입당 수수료 포함 $298 한도 | [legacy_weekly](research/picks/legacy_weekly.json) |
| 목표 90% | 신호가 있는 종목에 계좌의 90% 배분 | [target90](research/picks/target90.json) |
| 목표 95% | 신호가 있는 종목에 계좌의 95% 배분 | [target95](research/picks/target95.json) |
| 목표 99% | 신호가 있는 종목에 계좌의 99% 배분 | [target99](research/picks/target99.json) |
| 목표 100% | 신호가 있는 종목에 계좌의 100% 배분 | [target100](research/picks/target100.json) |

이전 완료 일봉의 `종가 > SMA200` 및 `SMA20 > SMA200`이면 매수 대상입니다.
SOXX 신호에는 SOXL, QQQ 신호에는 TQQQ를 사용합니다. 기존 종목 목록
SOXL·SOXS·TQQQ·SQQQ는 유지하지만 이 다섯 전략은 인버스 종목을 매수하지 않습니다.
주봉 가격을 사용하는 전략은 아닙니다. 월요일 휴장이면 그 주 첫 정규장에 판단합니다.

고비중형은 신호가 있는 두 종목에 균등 배분하고, 하나만 있으면 그 종목에 목표 금액을
전부 배분합니다. 무신호이면 현금 대기합니다. 매주 일부 매도·추가 매수로 비중을 조정하며
정수 주식과 현금 잔고를 적용합니다. 기존형에는 이 주간 비중 재조정이 없습니다.

종가 기준 -6%·+18% 감지 후 다음 정규장 시가에 청산하며 최대 보유는 20거래일입니다.
주중 청산 후 신규 진입은 다음 주 첫 거래일까지 기다립니다. 장중 손절이나 손실 6%를
보장하는 규칙은 아닙니다. 비용·수익률·낙폭·기간별 한계는 [비교 보고서](docs/strategy-report.md),
Git에 보존하는 지표는 [비교 결과 JSON](research/results/weekly-comparison.json)에 있습니다.

**기본 전략은 별도 LIVE 실행기로 연결할 수 있습니다.**
목표 90·95·99·100%형은 오프라인 비교용입니다. LIVE 설정과 계좌 조회 점검 절차는
[주간 기본 전략 LIVE](docs/live-trading.md)를 참고하세요.

## 설치와 백테스트

Python 3.12+와 uv를 사용합니다. 저장소 루트에서 실행하세요.

```sh
uv sync --frozen --python 3.12
uv run python -m research.compare_weekly_allocations --output data/weekly-comparison-new-run
uv run python -m research.audit_weekly_allocations data/weekly-comparison-new-run
```

다섯 전략 × 다섯 기간 × 기본/2배 비용으로 총 50회 실행합니다. 결과 폴더는 새로
생성하며 이미 있는 폴더를 덮어쓰지 않습니다. `plan.json`에 자료·코드 해시를 먼저 저장하고,
`summary.json`에 체결 원장·일별 평가액·지표, `audit.json`에 별도 원장 검산 결과를 저장합니다.
이전 평가 결과 파일은 실행에 필요하지 않습니다.

기본 입력 경로 `data/history/`에 다음 네 원본 일봉 CSV와 각각의 `.meta.json`이 필요합니다.
메타데이터의 가격 조정 여부·간격·CSV 해시와 여섯 종목의 거래일 누락을 검사합니다.

- `2021-04-01_2022-12-30-raw-1d.csv`
- `2022-11-01_2023-12-29-raw-1d.csv`
- `2024-03-08_2025-10-31-raw-1d.csv`
- `2025-05-01_2026-10-30-raw-1d.csv`

원본 자료가 다른 위치에 있으면 첫 명령에 `--history-dir /absolute/path/to/history`를
추가합니다. `data/`는 Git에서 제외하므로 새 체크아웃에는 원본 자료가 없습니다.
다운로드에는 `.env`의 토스증권 조회 자격증명과 API가 허용한 접속 환경이 필요합니다.

```sh
uv run python -m toss_trading.download \
  --start 2025-05-01 --end 2026-10-30 --interval 1d \
  --config config/strategy.toml \
  --output data/history/2025-05-01_2026-10-30-raw-1d.csv
```

다운로드는 원본 가격을 기본으로 사용하며 메타데이터를 함께 저장합니다. 동일한 방식으로
나머지 기간을 준비할 수 있습니다. 새로 내려받은 가격이 기존 자료와 충돌하면 병합을 중단하므로
새 자료로 평가했을 때 과거 저장 수치와 일치한다고 가정하지 않습니다.

## 조회 API와 대시보드

```sh
cp .env.example .env
uv run uvicorn toss_trading.api:create_app --factory --host 127.0.0.1 --port 8000
```

`.env`에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, 32자 이상의 `OPERATIONS_API_KEY`를
설정합니다. 조회 요청에는 `x-api-key`를 전달합니다. 계좌 조회는 `/v1/accounts`의
`accountSeq`를 `TOSS_ACCOUNT_SEQ`로 설정합니다. 키와 `.env`는 Git에 넣지 않습니다.

| 경로 | 기능 |
| --- | --- |
| `/health` | API 프로세스 상태 |
| `/docs` | 조회 API 명세 |
| `/v1/prices` | 현재가 조회 |
| `/v1/accounts` | 계좌 목록 |
| `/v1/holdings` | 보유 자산 조회 |
| `/v1/buying-power` | 매수 가능 금액 |
| `/v1/orders` | 주문 조회 |
| `/v1/paper` | 기존 모의 원장 조회 |
| `/dashboard/` | 원장 대시보드 |

대시보드는 별도의 `DASHBOARD_USERNAME`·`DASHBOARD_PASSWORD`를 사용합니다.
웹 대시보드는 LIVE 전략의 현금·보유 종목·주문·체결을 표시합니다.
Compose에서 LIVE 볼륨을 읽기 전용으로 연결합니다. [LIVE 배포와 대시보드](docs/live-trading.md)

## 검사와 배포

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

테스트는 실제 증권사·Slack 네트워크 연결을 차단합니다. 원본 시세·전체 체결 원장·
계좌별 `config/live.toml`·자격증명·가상환경은 Git에서 제외합니다.
조회 API 컨테이너는 `docker compose up --build -d`로 실행할 수 있습니다.
[아키텍처](docs/architecture.md), [조회 API 배포](docs/deployment.md),
[PAPER/LIVE 연결 상태](docs/live-trading.md)를 참고하세요.
