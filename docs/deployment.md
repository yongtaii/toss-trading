# 조회 API 배포

이 문서의 Compose는 조회 API와 기존 원장 대시보드를 배포한다.
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
기존 원장과 새 설정의 해시가 다르면 API는 원장 조회를 거부할 수 있다. 이전 원장을
보존하고 필요한 경우 별도 데이터베이스 경로를 사용한다. 기존 원장의 삭제·강제 변환은
이번 전략 정리에 포함하지 않았다.

컨테이너 기동 여부와 `/health` 응답은 전략 실행이나 실제 계좌 수익 검증을 의미하지 않는다.
현재 Compose에는 자동매매 PAPER 서비스가 없다. 백테스트 재현은 루트 README를 참고한다.
