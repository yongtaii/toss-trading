# 주간 기본 전략 LIVE

`weekly_baseline`은 SOXX·QQQ의 완료된 원본 일봉 200개로 상승 조건을 판단하고
SOXL·TQQQ를 거래한다. 신규 진입은 미국 기준 주 첫 정규장 개장 후 5분 동안 허용한다.
해당 구간의 매도호가로 지정가 매수를 낸다. 진입당 위험 예산은 계좌 평가액의 0.5%,
매수 한도는 수수료를 포함해 $298과 설정한 주문 한도 중 작은 값이다. 정수 수량만 주문한다.

`config/live.toml`의 최상위에 `weekly_target_allocation = "0.30"`을 설정하면
주 첫 거래일에 상승 신호가 있는 두 종목에 15%씩, 하나에만 신호가 있으면 30%를 배정한다.
초과 수량 축소를 먼저 처리하고 추가 매수 후 평균 매수가를 사용한다. 최초 보유일은 유지한다.
실시간 호가·체결, 기존 주문 한도와 일일 손실 가드, 매수 가능 현금에 따라 목표에 미달할 수 있다.
이 값은 실행기 설정으로 읽으며 기존 LIVE 장부를 그대로 사용한다. 기존 서버 파일에서 값을
생략하면 종전 진입 크기를 사용한다. 교체 후 `--status`에서 `weekly_target_allocation`을 확인한다.


실제 평균 매입가 대비 완료 일봉 종가가 -6% 또는 +18%에 닿거나,
20거래일을 보유했거나, 주간 상승 조건이 사라지면 정규장에 시장가 매도를 제출한다.
기존 포지션의 청산이 확인된 뒤 신규 매수를 검토한다. 개장을 놓친 청산은 이후 정규장
조회에서 시도한다. 백테스트는 시가 체결과 예상 비용을 사용하고 LIVE는 실제 호가·체결과
브로커 수수료를 사용하므로 체결 시각·가격·수량이 달라질 수 있다.
종가 손절은 야간 갭을 포함한 손실을 6%로 제한할 수 없다.

## Lightsail 설정과 조회 점검

기존 HTTPS 대시보드와 별도 Compose 프로젝트를 사용한다. Lightsail의 새 릴리스
폴더에서 다음을 실행한다.

```bash
sudo docker build -t toss-trading-live:weekly .
cp -n config/live.example.toml config/live.toml
(umask 077; touch .env.live)
```

`.env.live`에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`를 입력한다.
같은 API 자격증명으로 동시에 토큰을 발급하는 다른 워커가 있다면 먼저 정리한다.
`config/live.toml`에 실제 계좌의 `account_seq`, 배정 자금 `capital_usd`,
`max_order_usd`, 일일 신규 매수 중단 기준 `daily_loss_usd`를 입력한다.
전략 밖 보유 종목이 있으면 실제 USD 수량을 `protected_positions`에 명시한다.
계좌의 수동 주문과 조건 주문도 확인한다. 전략 종목의 기존 보유분은 새 원장에 자동 편입되지 않는다.

`enabled = false` 상태에서 아래 명령으로 계좌, 수수료, 일봉, 정규장 호가를 조회 점검한다.
정규장 밖에는 호가 확인이 생략된다.

```bash
sudo docker compose -p toss-trading-live -f compose.live.yaml run --rm live \
  .venv/bin/python -m toss_trading.live.runner --config config/live.toml
```

출력의 `read_only_preflight_passed`와 각 항목을 확인한다.
실행기에는 별도 LIVE 원장과 주문 복구 상태가 저장된다. 재배포에도 동일한
`toss-trading-live` 프로젝트와 데이터 볼륨을 사용한다.

## 활성화와 운영

실계좌 주문을 시작할 때 `enabled = true`로 설정하고 LIVE worker를 올린다.
처음에는 주문 제어 상태가 `pause`다. 계좌를 확인한 다음 `run`으로 전환하면
다음 진입 조건이 충족될 때 실제 주문이 제출된다.

```bash
sudo docker compose -p toss-trading-live -f compose.live.yaml up -d
sudo docker compose -p toss-trading-live -f compose.live.yaml exec live \
  .venv/bin/python -m toss_trading.live.runner --config config/live.toml --status
sudo docker compose -p toss-trading-live -f compose.live.yaml exec live \
  .venv/bin/python -m toss_trading.live.runner --config config/live.toml --control run
```

`--control pause`는 신규 매수를 멈추고 관리 중인 포지션의 전략 청산은 계속한다.
`--control flatten`은 정규장 중 관리 중인 포지션을 청산한다.
계좌나 주문을 대조할 수 없으면 신규 전송을 보류한다. 주문 전송 결과가 불명확한 경우
자동 재주문을 피하고 브로커 주문 ID를 확인해 `--attach`로 연결한다.
`--attach` 작업을 할 때는 먼저 워커를 중지하고 같은 Compose 프로젝트에서 실행한다.
웹 대시보드에는 LIVE 전략의 현금·평가액·보유 종목·실제 주문과 체결이 표시된다.
토스 계좌의 매수 가능 금액은 별도 항목이며 마지막 계좌 대조 시점의 값이다.

대시보드 API의 Compose 명령에 `compose.dashboard-live.yaml`을 추가한다.
이 설정은 `toss-trading-live` 프로젝트의 기본 LIVE 볼륨을 읽기 전용으로 마운트한다.
대시보드에서 실매수 허용 표시는 실행기의 주문 제어가 `run`이라는 뜻이다.
