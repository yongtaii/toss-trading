---
name: tt-pat-push
description: Prepare and verify a GitHub HTTPS push to yongtaii/toss-trading main with the user's personal PAT. Use when asked to push Toss Trading commits, generate a copy-paste PAT push command, or troubleshoot its GitHub authentication.
---

# Toss Trading 개인 PAT 푸시

Mac에서 `git worktree list`, `git status -sb`, `git log -1 --oneline`,
`git remote get-url --push origin`, `git ls-remote origin refs/heads/main`으로
대상 워크트리, 커밋, 원격을 확인한다. 기본 체크아웃과 Codex 워크트리의 변경을
섞지 않는다. 대상 원격은 `https://github.com/yongtaii/toss-trading.git`이고,
푸시 대상은 `main`이다. 커밋 전 변경이 있으면 푸시에 포함되지 않는다고
알린다. 스테이징이나 커밋은 해당 작업이 요청됐을 때 진행한다.

원격 `main`이 이미 원하는 커밋을 가리키면 확인 결과를 알려준다.
개인 PAT 인증이 필요하면 실제 경로와 검토한 커밋 해시를 채워, **Mac zsh 터미널**에
복사할 아래 블록을 제공한다. 토큰은 사용자가 터미널의 숨김 입력에 붙여넣도록
안내한다. 토큰을 채팅, 명령 인수, 원격 URL이나 파일로 받지 않는다.

```zsh
(
  set +x
  cd '/absolute/path/to/toss-trading-worktree' || exit 1
  revision='REVIEWED_COMMIT_SHA'
  test "$(git remote get-url --push origin)" = \
    'https://github.com/yongtaii/toss-trading.git' || exit 1
  expected=$(git rev-parse --verify "$revision^{commit}") || exit 1

  read -rs 'toss_pat?개인 GitHub PAT: ' || exit 1
  printf '\n'
  test -n "$toss_pat" || exit 1
  export GH_TOKEN="$toss_pat"
  unset toss_pat

  login=$(gh api user --jq .login) || exit 1
  if [[ "$login" != yongtaii ]]; then
    printf '이 PAT의 계정은 %s입니다. yongtaii 계정의 PAT를 사용하세요.\n' "$login"
    exit 1
  fi

  git_personal() {
    git -c credential.helper= -c 'credential.helper=!gh auth git-credential' "$@"
  }

  git_personal push origin "$expected:refs/heads/main" || exit 1
  remote_line=$(git_personal ls-remote --exit-code origin refs/heads/main) || exit 1
  actual=${remote_line%%$'\t'*}
  if [[ "$actual" != "$expected" ]]; then
    printf '원격 main 확인 필요: %s\n' "$actual"
    exit 1
  fi
  printf '원격 main 확인 완료: %s\n' "$actual"
)
```

`401`이면 토큰 입력, 만료, 폐기 여부를 확인한다. 인증이 성공한 뒤 `403`이
나면 저장소 선택, Contents 쓰기 권한, 브랜치 보호 정책을 확인한다.
non-fast-forward 거절이 나오면 원격과 로컬 커밋의 차이를 확인한다.
강제 푸시나 저장된 회사 계정 자격증명 변경을 사용하지 않는다.
사용자가 푸시 후 확인을 요청하면 원격 SHA를 직접 조회해 보고한다.
