# Lightsail PAPER 대시보드 배포

구현된 구성은 Linux 서버 한 대에서 Caddy(HTTPS) → FastAPI(대시보드·원장 조회),
PAPER worker(증권사 시세 조회)와 영속 원장을 운용합니다.
대시보드의 자산·주문은 PAPER 전용이며 실제 주문 실행은 차단돼 있습니다.
AWS 리소스 생성과 실제 원격 배포는 아직 수행하지 않았습니다.

## 로컬 실행

`.env.example`을 참고해 `.env`를 설정합니다. `OPERATIONS_API_KEY`는 32자 이상,
`DASHBOARD_PASSWORD`는 별도의 무작위 16자 이상 값으로 설정하세요.
브라우저는 `DASHBOARD_USERNAME`(기본 `admin`)과 `DASHBOARD_PASSWORD`로 로그인합니다.
비밀번호를 프론트 소스나 URL에 넣지 않습니다.

```sh
docker compose --profile paper up --build -d
```

브라우저에서 `http://127.0.0.1:8000/dashboard/`를 엽니다. Docker 없이 실행할 때는
README의 uvicorn·runner 명령을 사용하세요. `GET /health`는 API 생존 여부만 확인합니다.
원장이 없으면 worker 첫 실행 전까지 대시보드가 조회 오류를 표시합니다.

기본 Compose의 API에는 증권사 키를 전달하지 않습니다. worker 하나가 토큰을 소유하고
API는 원장만 조회하므로 대시보드 갱신이 증권사 호출 한도나 토큰에 영향을 주지 않습니다.
실제 계좌 조회 API까지 컨테이너에서 쓰려면 API용 별도 client를 발급해 주입하세요.
worker와 같은 client로 smoke/download를 동시에 실행하지 마세요.

## Lightsail 서버 준비

1. Linux 인스턴스와 고정 공인 IPv4를 준비하고, 그 IP를 토스증권 허용 목록에 등록합니다.
2. Docker Engine과 Compose 플러그인을 설치합니다. 저장소를 서버에 복사하거나 clone합니다.
   `.env`, `data/`와 개인 키는 Git·이미지에 포함하지 않습니다.
3. 도메인의 A 레코드를 고정 IP로 설정합니다. AAAA 레코드는 IPv6가 실제 연결된 경우에만 설정합니다.
4. Lightsail 및 OS 방화벽에서 80/443을 허용하고 SSH는 관리자의 IP로 제한합니다.
   8000과 PostgreSQL 5432는 외부에 열지 않습니다.
5. 서버의 `.env`를 설정한 뒤 `chmod 600 .env`를 적용합니다.

HTTPS를 위해 다음 두 값이 추가로 필요합니다.

```dotenv
DASHBOARD_DOMAIN=trading.your-domain.com
ACME_EMAIL=you@your-domain.com
```

`DASHBOARD_DOMAIN`에는 프로토콜과 경로 없이 호스트명만 입력합니다.
SQLite 기본값은 `sqlite:///data/paper.db`이며, 컨테이너에서는 `paper_data` 볼륨을 공유합니다.
현재 전략 설정과 일치하는 원장을 사용해야 합니다. 로컬의 과거 원장을 서버에서 이어 쓸 때는
동일 설정·DB를 명시적으로 이전하세요. 빈 볼륨으로 실행하면 새 PAPER 자금으로 시작합니다.

## 배포와 확인

```sh
docker compose -f compose.yaml -f compose.aws.yaml --profile paper config --quiet
docker compose -f compose.yaml -f compose.aws.yaml --profile paper up --build -d
docker compose -f compose.yaml -f compose.aws.yaml --profile paper ps
docker compose -f compose.yaml -f compose.aws.yaml --profile paper logs --tail=100 caddy api paper
```

Caddy가 도메인에 대한 인증서를 자동 발급합니다. 브라우저에서
`https://<DASHBOARD_DOMAIN>/dashboard/`를 열고 로그인합니다.
외부 reverse proxy는 `/dashboard*`만 전달하며 `/docs`, `/v1/*`, `/health`는 공개하지 않습니다.
최초 로그인 뒤 총자산·PAPER 표시·갱신 시각을 확인하세요. 장외에는 종목별 시세가 없거나 오래될 수 있습니다.
수집기 heartbeat와 시세 수집 성공은 별도 상태입니다. 최근 수집 결과·오류·종목별 시각을 함께 확인합니다.
업데이트 후 worker를 재시작하면 모니터링용 테이블 두 개가 추가되고 heartbeat와 최신 조건 기록이 시작됩니다.
기존 원장 데이터나 전략 설정을 변경할 필요는 없습니다.

기존 API 키 기반 조회는 SSH 터널로 이용할 수 있습니다.

```sh
ssh -L 8000:127.0.0.1:8000 ubuntu@<STATIC_IP>
```

## 백업과 복구

SQLite 파일을 실행 중에 단순 복사하지 말고 SQLite backup API를 사용합니다.
아래 예시는 컨테이너 기본 DB 경로일 때의 일관된 백업을 호스트로 가져옵니다.
`PAPER_DATABASE_URL`을 바꿨으면 원본 경로를 맞추세요.

```sh
mkdir -p backups
docker compose exec -T api /app/.venv/bin/python -c 'import sqlite3; src=sqlite3.connect("file:/app/data/paper.db?mode=ro", uri=True); dst=sqlite3.connect("/tmp/paper-backup.db"); src.backup(dst); dst.close(); src.close()'
docker compose cp api:/tmp/paper-backup.db backups/paper-backup.db
```

설정 TOML·배포 커밋 SHA도 함께 보관하고 백업은 서버 외부에 별도 저장합니다.
비교 원장을 쓰면 해당 DB도 각각 백업하세요. PostgreSQL은 `pg_dump`/복원 절차를 별도로 운영합니다.
서버 스냅샷은 보조 수단으로 사용합니다.

복구 시 worker와 API를 중지하고, 원장을 복원한 뒤 복원본과 동일한 전략 설정으로 재시작합니다.
원본을 덮어쓰기 전에 별도 보관하고, 복구 환경에서 자산·주문·이벤트를 확인하세요.
`docker compose down -v`는 원장과 인증서 볼륨을 삭제하므로 운영 종료 명령으로 사용하지 않습니다.

## 이후 자동 배포

현재 CI는 lint·테스트·이미지 빌드까지 수행합니다. CD는 아직 추가하지 않았습니다.
향후 CI 성공 이미지의 커밋 SHA를 GHCR에 게시하고, 서버가 정확한 SHA 이미지를 pull하도록 구성합니다.
DB와 설정의 호환성을 확인하고 이전 SHA와 백업을 확보한 뒤 배포합니다.
현재 `metadata.create_all`은 초기 스키마 생성만 지원하며 버전별 마이그레이션을 수행하지 않습니다.
