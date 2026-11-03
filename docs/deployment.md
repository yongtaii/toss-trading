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

## Mac에서 한 줄 배포

이미 운영 중인 Lightsail에 업데이트를 배포할 때 사용한다. GitHub `main`의
커밋을 Mac에서 받아 SSH로 보내므로, Lightsail에서 GitHub PAT를 입력할 필요가
없다. 서버는 Docker Compose, `python3`, `flock`과 `sudo -n docker` 접근이 필요하다.

Mac에서 배포용 키를 만든다.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/toss-lightsail -C toss-lightsail
cat ~/.ssh/toss-lightsail.pub
```

Lightsail 브라우저 SSH에서 `~/.ssh`를 권한 700으로 만들고,
출력한 **공개키 한 줄**을 `~/.ssh/authorized_keys`에 추가한다.
파일 권한을 600으로 맞춘다. Mac의 `~/.ssh/config`에 아래 항목을 넣는다.
`LIGHTSAIL_PUBLIC_IP`는 Lightsail의 공인 IP로 바꾼다.

```sshconfig
Host toss-lightsail
    HostName LIGHTSAIL_PUBLIC_IP
    User ubuntu
    IdentityFile ~/.ssh/toss-lightsail
    IdentitiesOnly yes
```

처음 접속할 때 Lightsail 브라우저 SSH에서
`sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`로 본 호스트 지문을
Mac에서 표시된 지문과 대조한다. 키에 암호를 설정했다면
`ssh-add ~/.ssh/toss-lightsail`로 에이전트에 등록한다.

```bash
ssh toss-lightsail 'hostname && sudo -n docker compose ls'
```

이후 Mac의 저장소 루트에서 필요한 서비스만 배포한다.

```bash
./deploy/lightsail.sh dashboard
./deploy/lightsail.sh live
```

대시보드는 임시 컨테이너에서 LIVE 원장 HTTP 200과 실행기 상태를 확인한 후
API만 교체한다. 기존 Caddy와 LIVE 실행기는 유지한다. LIVE 실행기는 정규장 밖이고
미결 주문이 없을 때 배포한다. 기존 계좌 설정과 LIVE 원장·주문 제어 상태를
이어 쓴다. 전략의 설정값을 바꾸려면 서버 설정을 별도로 변경해야 한다.
교체 후 검사에 실패하면 이전 이미지를 복구한다.

## GHCR 이미지 자동 배포

`main`에 푸시하면 **Publish Dashboard**가 CI를 통과한 소스로 amd64·arm64
이미지를 만든다. GHCR에 커밋별 `sha-<commit>` 태그를 게시하고,
그 커밋이 여전히 `main`의 최신 커밋이면 `dashboard` 태그를 갱신한다.

LIVE를 업데이트하려면 해당 커밋의 **Publish Dashboard**가 성공한 뒤
GitHub의 **Actions → Deploy LIVE → Run workflow → main → Run workflow**를
실행한다. 워크플로는 기존 커밋 이미지를 `live` 태그로 지정한다.
이미지 게시와 실제 서버 교체 결과는 각각 GitHub Actions와 Lightsail 로그에서 확인한다.

### Lightsail 최초 설정

이미 LIVE 원장을 읽는 운영 대시보드와 LIVE 실행기가 올라간 서버에서 설정한다.
GHCR 패키지가 비공개이면 서버에서 패키지 조회 권한이 있는 GitHub classic PAT
(`read:packages`)로 Docker를 로그인한다. 토큰은 비밀번호 프롬프트에 입력한다.

```bash
sudo docker login ghcr.io -u yongtaii
```

자동 배포 파일이 올라간 `main`을 Lightsail의 새 폴더에 한 번 받아,
해당 폴더에서 `./deploy/install-image-updates.sh`를 실행한다.
설치 이후 서버는 Git 저장소 대신 GHCR 이미지를 가져온다.
설치 파일 자체를 바꿨을 때는 새 코드를 받고 설치 명령을 다시 실행한다.

두 systemd 타이머가 각각 새 이미지를 검사한다. 같은 이미지면 그대로 종료한다.
대시보드는 임시 컨테이너에서 LIVE 원장 조회를 통과한 후 API만 교체한다.
LIVE는 정규장 밖이고 미결 주문이 없을 때 교체한다. 현재 Compose 설정, 볼륨,
LIVE 계좌 설정과 주문 제어 상태를 이어 쓴다. Compose나 서버 설정 변경은 별도로 반영한다.

```bash
systemctl list-timers 'toss-image-update@*'
sudo journalctl -u toss-image-update@dashboard.service -n 50 --no-pager
sudo journalctl -u toss-image-update@live.service -n 50 --no-pager
```

교체 후 검증이 실패하면 이전 이미지로 복구하고, 실패한 이미지의 반복 교체를
막는다. 원인을 해결해 새 이미지를 게시하면 타이머가 새 이미지를 검사한다.
LIVE 배포를 보류하려면 `sudo systemctl disable --now toss-image-update@live.timer`를
실행한다. 실행 중인 거래 서비스의 주문 제어는 LIVE 명령으로 관리한다.
