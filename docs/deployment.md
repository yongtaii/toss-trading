# Lightsail 배포 준비 계획

현재 로컬 PAPER/백테스트, SQLite/PostgreSQL URL 지원, 컨테이너 정의와 CI를 제공합니다. AWS 리소스 생성·원격 배포·
GitHub 저장소 생성·Slack 메시지 전송은 수행하지 않습니다.

1. Lightsail Linux 인스턴스에 Docker Engine/Compose를 설치하고 고정 공인 IP를 연결합니다.
   해당 IP를 토스증권 허용 목록에 등록합니다.
2. 서버의 소유자만 읽을 수 있는 `.env` 또는 AWS Secrets Manager로 자격증명을 전달합니다.
   이미지, Git, GitHub Actions 로그에 키를 넣지 않습니다. 키 권한과 회전 절차를 기록합니다.
3. client당 단일 API 인스턴스로 시작합니다. `docker compose up --build -d` 후 `/health`와
   인증된 조회를 점검합니다. `/health`만으로 증권사 연결 성공을 판단하지 않습니다.
4. 외부 운영 접근은 먼저 SSH 터널로 제한합니다. 대시보드 공개 시 TLS reverse proxy와
   사용자 인증·권한·접근 제어·요청 제한을 추가합니다. PostgreSQL 포트는 외부에 열지 않습니다.
5. 운영 저장소 검증 단계에서 백업·복원, 디스크/메모리/시계/로그 관리와 토큰 단일 소유권을 검증합니다.
6. 후속 CD는 CI 성공 이미지의 커밋 SHA를 GHCR에 게시하고, GitHub Environment 승인 후
   Lightsail에서 정확한 SHA 이미지를 pull하도록 구성합니다. 배포 키는 별도 Secret으로 관리하고
   호스트 키 검증을 켭니다. health 실패 시 이전 SHA로 롤백합니다.
7. Slack webhook 및 배포 Secrets는 실제 운영 계정 정보가 확정되는 단계에서 연결합니다.

현재 workflow는 lint·테스트·이미지 빌드만 수행하며 자동 배포하지 않습니다.
DB 마이그레이션 호환성·백업·롤백 절차를 CD의 필수 조건으로 추가해야 합니다.

PAPER worker는 `docker compose --profile paper up --build -d paper`로 단독 실행할 수 있습니다.
API와 동시에 실제 조회를 사용하려면 별도 발급한 client 또는 공유 토큰 소유권을 구성해야 합니다.
같은 client의 토큰을 각 프로세스가 재발급하면 기존 토큰이 무효화됩니다.
API와 worker는 같은 `paper_data` volume과 설정 파일을 읽습니다. Slack은 worker에서
명시적으로 `--slack`을 지정한 경우에만 전송합니다.
