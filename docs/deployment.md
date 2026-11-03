# 조회 API 배포

이 문서의 Compose는 조회 API와 LIVE 전략 대시보드를 배포한다.
주간 기본 전략 LIVE는 별도 Compose 프로젝트로 배포한다. [LIVE 배포 절차](live-trading.md)

```sh
cp .env.example .env
# .env에 조회 자격증명과 별도 API/대시보드 비밀번호를 설정한다.
docker compose up --build -d
```

기본 API는 `127.0.0.1:8000`에만 노출한다. 외부 HTTPS가 필요하면 DNS와
`DASHBOARD_DOMAIN`·`ACME_EMAIL`을 설정한 뒤 아래 구성을 사용한다.

```sh
docker compose -f compose.yaml -f compose.aws.yaml up --build -d
```

키·환경 파일·실제 계좌별 설정·원장은 Git과 컨테이너 이미지에 넣지 않는다.
LIVE 원장과 주문 제어 상태를 표시하려면 API 배포에
`-f compose.dashboard-live.yaml`을 추가한다. 이 파일은 기존 LIVE 볼륨을 읽기 전용으로
연결한다.

컨테이너 기동 여부와 `/health` 응답은 전략 실행이나 실제 계좌 수익 검증을 의미하지 않는다.
현재 Compose에는 자동매매 PAPER 서비스가 없다. 백테스트 재현은 루트 README를 참고한다.
