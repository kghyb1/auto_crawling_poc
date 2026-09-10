#!/usr/bin/env bash
# 처음 한 번만 실행하는 설치 스크립트 (가상환경 + 라이브러리 + 설정 파일)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

echo "[1/4] 파이썬 확인"
if ! command -v python3 >/dev/null 2>&1; then
    echo "  python3 를 찾을 수 없습니다. 3.10 이상을 설치하세요." >&2
    exit 1
fi
python3 --version

echo "[2/4] 가상환경 만들기 (.venv)"
if [ ! -x ".venv/bin/python" ]; then
    python3 -m venv .venv
else
    echo "  이미 있습니다. 건너뜁니다."
fi

echo "[3/4] 라이브러리 설치"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "[4/4] 설정 파일 준비"
.venv/bin/python bot.py init

cat <<'MSG'

설치가 끝났습니다. 다음 순서로 진행하세요.
  1) config/targets.yaml 에 수집할 홍보사이트 주소를 넣고 enabled: true 로 바꾸기
  2) .venv/bin/python bot.py import-targets
  3) .venv/bin/python bot.py start          # 또는 systemd 등록 (scripts/linux/illegal-site-bot.service)
MSG
