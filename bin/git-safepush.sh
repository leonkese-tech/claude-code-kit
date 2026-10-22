#!/bin/sh
# safepush.sh — 안전한 순차 푸시 (force-push 충돌/엉킴 방지)
#
#   사용:  git safepush ["커밋 메시지"]
#          (메시지 생략 시 'chore: 작업 저장')
#
# 동작: ① 진행 중 rebase/merge 있으면 즉시 중단(엉킴 방지)
#       ② 현재 작업 커밋(스테이징된 것만 / 비었으면 추적파일 수정·삭제만)
#       ③ 원격에 **새 커밋이 있을 때만** rebase → push
#       ④ 다른 세션이 먼저 push해 거부되면 자동 재정렬 후 재시도(최대 5회)
#       ⑤ 실패하면 **실패한 진짜 이유**를 구분해 알린다(작업은 이미 커밋돼 안전)
#
#   ★ force-push 안 함 / 멈춘 rebase 위에 덧쌓지 않음 / 작업 유실 없음
#   ★ stash·autostash·checkout 을 쓰지 않는다 — 미커밋 변경이 있으면 재정렬하지 않고 멈춘다
set -eu

msg="${1:-chore: 작업 저장}"
gitdir="$(git rev-parse --git-dir)"
branch="$(git rev-parse --abbrev-ref HEAD)"

# 올릴 자리(target)
#    ★ 데스크톱 워크트리 브랜치(claude/*)는 원격에 같은 이름이 없다.
#      예전엔 `origin/$branch` 가 없어 ahead 계산이 실패 → 0 → `git add -u` 가 돌아
#      **개행만 다른 추적 파일 수십 개**를 사용자 메시지로 담을 뻔했고, 푸시도 `origin $branch` 라
#      공유 main 이 아닌 새 원격 브랜치로 갔다.
#      → 원격에 같은 이름이 있으면(main·장기 브랜치) 예전 그대로, 없으면(워크트리·detached HEAD)
#        기준을 origin/main 으로 잡고 **main 에 fast-forward 로만** 올린다.
#      ⚠️ detached HEAD 를 그대로 두면 `origin/HEAD`(심볼릭)가 걸려 원격에 'HEAD' 브랜치가 생긴다.
if [ "$branch" != "HEAD" ] && git rev-parse -q --verify "refs/remotes/origin/$branch" >/dev/null; then
  target="$branch"
else
  target="main"
  echo "[대상] $branch → origin/main (원격에 같은 이름이 없는 브랜치 — 공유 main 에 fast-forward 로 올립니다)"
fi

# ① 진행 중인 rebase/merge면 중단 ('멈춘 rebase 위에 덧쌓기' 차단)
if [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ]; then
  echo "[중단] rebase 진행 중 — 'git rebase --abort' 또는 '--continue'로 정리 후 다시 실행"
  exit 1
fi
if [ -f "$gitdir/MERGE_HEAD" ]; then
  echo "[중단] merge 진행 중 — 먼저 정리 후 다시 실행"
  exit 1
fi

# ② 커밋 (변경 없으면 통과)
#    ★ 예전엔 무조건 `git add -A` 라, **다른 세션이 편집 중이던 파일과
#      임시 스크립트까지 통째로** 담아 엉뚱한 메시지의 커밋에 섞여 들어갔다.
#      → ⓐ 이미 스테이징된 게 있으면 **그것만** 커밋(호출자의 의도 존중)
#        ⓑ 스테이징이 비었을 때만 `git add -u`(추적 파일의 수정·삭제만; 새 파일은 제외)
#      새 파일은 의도적으로 `git add <파일>` 한 뒤 safepush 를 부른다(임시파일 오커밋 방지).
#    ★ 에이전트가 **자기 파일을 먼저 `git commit` 한 뒤** safepush 를 불렀다.
#      스테이징이 비어 ⓑ `git add -u` 가 돌아, **다른 세션의 미커밋 파일들**이 엉뚱한 메시지로
#      origin 에 올라갔다. → ⓒ 이미 **미푸시 커밋이 있으면** 호출자가 커밋을 끝낸 것으로
#      보고 `add -u` 를 하지 않는다(푸시만). 남은 추적 파일을 더 담으려면 `git add <파일>` 로 명시.
#    ★ ⓓ 워크트리 브랜치(target≠branch)에서는 `git add -u` 를 **아예 하지 않는다**
#      (새 체크아웃마다 개행만 다른 추적 파일이 수십 개 생긴다 — 내 작업이 아니다).
#      ⓔ 기준 계산이 실패하면 0 으로 치지 않는다(예전 `|| echo 0` 이 곧 `add -u` 였다 — fail-closed).
if git diff --cached --quiet; then
  if [ "$target" != "$branch" ]; then
    echo "[정보] 워크트리 브랜치 — 추적 파일 자동 담기(git add -u)를 하지 않습니다(담을 파일은 'git add <파일>' 로 명시)"
    if ahead="$(git rev-list --count "origin/$target..HEAD" 2>/dev/null)" && [ "$ahead" -eq 0 ]; then
      echo "[정보] 올릴 커밋이 없습니다 — 내 파일만 'git add <파일>' 로 담은 뒤 다시 실행하세요"
      exit 0
    fi
  elif ! ahead="$(git rev-list --count "origin/$target..HEAD" 2>/dev/null)"; then
    echo "[정보] origin/$target 기준을 계산하지 못해 추적 파일 자동 담기(git add -u)를 건너뜁니다"
  elif [ "$ahead" -gt 0 ]; then
    echo "[정보] 미푸시 커밋 $ahead 개가 이미 있어 추적 파일 자동 담기(git add -u)를 건너뜁니다 — 남의 미커밋 보호"
  else
    git add -u
  fi
fi
if git diff --cached --quiet; then
  echo "[정보] 새 변경 없음 — 미푸시 커밋만 올립니다"
else
  echo "[커밋 대상]"
  git diff --cached --name-status | sed 's/^/  /'
  git commit -m "$msg" >/dev/null
  echo "[커밋] $msg"
fi

# ③④ fetch → (필요할 때만) rebase → push, 경쟁 시 재시도(최대 5회)
#
#  ★ '없는 충돌'을 보고하던 버그.
#    예전엔 `git rebase` 가 0 이 아니면 **무조건 '충돌'** 이라고 했다. 그런데 rebase 는
#    충돌 말고도 실패한다 — 실제 원인은 늘 이것이었다:
#        error: cannot rebase: You have unstaged changes.
#    다른 세션이 편집 중인 파일 하나만 있어도 rebase 가 거부된다.
#    게다가 '충돌 파일' 로 찍던 `git diff HEAD origin/main` 은 **내가 방금 커밋한 파일**
#    목록이라, 멀쩡한 내 파일들이 충돌로 표시됐다(원격과 갈라진 게 전혀 없는데도).
#    → ⓐ 원격에 새 커밋이 없으면 **rebase 를 아예 하지 않는다**(대부분의 경우)
#      ⓑ rebase 가 필요한데 작업트리가 더러우면 **그 사실 그대로** 알린다
#      ⓒ 진짜 충돌은 abort **전에** `--diff-filter=U` 로 잡아서 진짜 파일만 보여준다
#      ⓓ 그 밖의 실패는 git 이 뱉은 문장을 그대로 보여준다(추측하지 않는다)
i=1
while [ "$i" -le 5 ]; do
  #  ⚠️ fetch 실패를 손대지 않으면 `set -e` 가 스크립트를 그대로 죽여, 커밋은 됐는데
  #     아무 안내 없이 git 의 fatal 만 남는다(네트워크·VPN·인증이 흔한 원인).
  if ! fetch_err="$(git fetch -q origin 2>&1)"; then
    echo "[중단] 원격 조회 실패 — 작업은 커밋돼 안전합니다(푸시만 남음)."
    echo "       git: $(echo "$fetch_err" | grep -v '^remote:' | head -3 | tr '
' ' ')"
    echo "       네트워크·VPN·인증을 확인한 뒤 'git safepush' 를 다시 실행하세요."
    exit 6
  fi

  behind="$(git rev-list --count "HEAD..origin/$target" 2>/dev/null || echo 0)"
  if [ "$behind" -gt 0 ]; then
    #  rebase 는 깨끗한 작업트리를 요구한다. 남의 미커밋을 stash 하지 않는다
    #  (stash·checkout 이 남의 작업을 쓸어간 사고를 피하려는 것).
    dirty="$(git status --porcelain --untracked-files=no | sed 's/^...//' | tr '\n' ' ')"
    if [ -n "$dirty" ]; then
      echo "[중단] 원격에 새 커밋 $behind 개가 있는데, 미커밋 변경이 있어 재정렬할 수 없습니다."
      echo "       내 작업은 커밋돼 안전합니다(푸시만 남음)."
      #  개행(CRLF)만 다른 파일은 따로 센다 — 새 워크트리마다 수십 개라 진짜 편집이 묻힌다.
      real="$(git -c core.quotepath=false diff HEAD --ignore-cr-at-eol --name-only 2>/dev/null | tr '\n' ' ')"
      n_all="$(git diff HEAD --name-only 2>/dev/null | wc -l | tr -d ' ')"
      n_real="$(git diff HEAD --ignore-cr-at-eol --name-only 2>/dev/null | wc -l | tr -d ' ')"
      echo "       미커밋 파일: ${real:-(없음)}"
      if [ "$n_all" -gt "$n_real" ]; then
        echo "       + 개행(CRLF)만 다른 파일 $((n_all - n_real))개 — 편집이 아니라 레포 줄바꿈 정규화 대기분"
      fi
      echo "       ※ 다른 세션이 작업 중인 파일일 수 있습니다 — 남의 파일은 건드리지 말고,"
      echo "         내 파일만 'git add <파일>' 로 담은 뒤 'git safepush' 를 다시 실행하세요."
      #  워크트리는 개행 파일 때문에 늘 여기로 온다 — 미커밋을 건드리지 않고 옮기는 길을 알려 준다.
      #  reset --keep 은 미커밋 파일을 덮어써야 하면 **거부**하고, cherry-pick 도 겹치면 멈춘다.
      if [ "$target" != "$branch" ]; then
        h="$(git rev-parse --short HEAD)"
        echo "       ※ 워크트리: 미커밋은 그대로 두고 내 커밋만 main 위로 옮기는 법(겹치는 파일이 있으면 git 이 거부)"
        echo "         git reset --keep origin/main && git cherry-pick origin/main..$h"
        echo "         → 그다음 'git safepush' 다시 · 충돌하면 'git cherry-pick --abort' 후 'git reset --keep $h' 로 원위치"
      fi
      exit 4
    fi
    if ! rebase_err="$(git rebase "origin/$target" 2>&1)"; then
      #  ⚠️ abort 하면 흔적이 사라진다 — 충돌 파일은 **지금** 잡아 둔다.
      conflicts="$(git diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ')"
      git rebase --abort 2>/dev/null || true
      if [ -n "$conflicts" ]; then
        echo "[중단] rebase 충돌 — 작업은 커밋돼 안전합니다."
        echo "       충돌 파일: $conflicts"
        echo "       해당 파일만 정리(또는 'git pull --rebase' 해소) 후 다시 'git safepush'"
      else
        echo "[중단] rebase 실패(충돌 아님) — 작업은 커밋돼 안전합니다."
        echo "       git: $(echo "$rebase_err" | head -3 | tr '\n' ' ')"
      fi
      exit 2
    fi
  fi

  #  refspec 을 명시한다 — 워크트리 브랜치는 HEAD 를 main 에 올린다. force(+) 없음:
  #  main 이 HEAD 의 조상이 아니면 git 이 거부하고, 아래에서 재정렬 후 재시도한다.
  if push_err="$(git push origin "HEAD:refs/heads/$target" 2>&1)"; then
    if [ "$target" = "$branch" ]; then
      echo "[완료] 푸시 성공: $branch @ $(git rev-parse --short HEAD)"
    else
      echo "[완료] 푸시 성공: $branch → main @ $(git rev-parse --short HEAD)"
      echo "       ※ 메인 체크아웃의 main 은 그대로입니다 — 반영은 그쪽에서 'git pull'(또는 다음 safepush)"
    fi
    exit 0
  fi
  #  ⚠️ 푸시 실패를 전부 '경쟁' 으로 보면, 인증·네트워크 오류일 때 5번 헛돌고
  #     엉뚱한 안내를 남긴다. 원격이 앞선 경우만 재시도한다.
  if echo "$push_err" | grep -qiE 'non-fast-forward|fetch first|behind'; then
    echo "[재시도 $i/5] 다른 세션이 먼저 푸시함 — 재정렬 후 다시 시도"
    i=$((i + 1))
    sleep 2
    continue
  fi
  echo "[중단] 푸시 실패 — 작업은 커밋돼 안전합니다."
  echo "       git: $(echo "$push_err" | grep -v '^remote:' | head -3 | tr '\n' ' ')"
  exit 5
done

echo "[중단] 5회 재시도 실패 — 잠시 후 다시 실행하세요"
exit 3
