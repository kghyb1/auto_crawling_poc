# 불법사이트 URL 수집 봇

불법홍보사이트(도박·성인·불법복제 사이트를 배너로 홍보하는 페이지)를 주기적으로 돌면서,
거기서 홍보되는 **불법사이트 URL을 뽑아 엑셀로 정리**하는 봇입니다.
워크스테이션 PC 에서 24시간 상주하며, 수집을 **ON/OFF** 로 언제든 켜고 끌 수 있습니다.

```
 config/targets.yaml          ┌──────────── 봇 프로세스 (24시간 상주) ────────────┐
 (홍보사이트 목록)      ──▶   │                                                  │
                              │  ① 페이지 수집   HTTP / (선택)브라우저 렌더링     │
                              │  ② 링크 추출     a href · 배너 img · onclick ·   │
                              │                  data-href · iframe · script ·   │
                              │                  본문 텍스트(abc[.]com 형태)      │
                              │  ③ 경유 추적     /go?url=... → 최종 도착지        │
                              │  ④ 판별/점수     rules.yaml 키워드·도메인 규칙    │
                              │  ⑤ 저장          SQLite (신규/중복/발견이력)      │
                              │  ⑥ 엑셀 출력     시트 6개                        │
                              │  ⑦ 생존 확인     아직 열려 있는지 주기 점검       │
                              └──────────────────────────────────────────────────┘
                                        ▲                          │
                          ON/OFF·즉시실행 (CLI·웹화면)              ▼
                                                        data/exports/*.xlsx
```

---

## ⚠️ 사용 전에 반드시 확인하세요

이 도구는 **불법사이트를 신고·차단·모니터링하기 위한 자료 수집** 용도로 만들어졌습니다.
그래서 다음과 같이 동작합니다.

| 하는 일 | 하지 않는 일 |
|---|---|
| 공개된 페이지의 HTML 을 받아 **링크(URL)만** 추출 | 로그인·인증 우회, 회원가입, 결제 |
| 수집한 URL 을 목록으로 정리 | 수집한 사이트에 자동 접속·이용·가입 |
| 사이트가 아직 열려 있는지 HEAD/GET 1회 확인 | 부하를 주는 반복 요청, 공격, 차단 우회 |
| `robots.txt` 준수 (기본값) | 대상 사이트의 콘텐츠 저장·재배포 |

- 수집한 URL 은 **방송통신심의위원회 신고, 경찰 신고, 사내 차단 목록 등 적법한 목적**으로만 사용하세요.
- 수집 대상 서버에 부담을 주지 않도록 호스트별 요청 간격(`per_host_delay_seconds`)과
  동시 요청 수(`concurrency`)가 기본적으로 보수적으로 설정되어 있습니다. 함부로 올리지 마세요.
- 자동 판별은 **키워드·도메인 형태에 기반한 추정**입니다. 점수가 높다고 곧 불법이 확정되는 것은
  아니므로, 신고 전에 사람이 확인하는 절차를 반드시 두세요. (`처리 상태` 컬럼과 `mark` 명령이 그 용도입니다.)

---

## 설치

필요한 것: **Python 3.10 이상** (Windows 는 설치 시 `Add python.exe to PATH` 체크)

### Windows

```bat
git clone <이 저장소> auto_crawling_poc
cd auto_crawling_poc
scripts\windows\setup.bat
```

`setup.bat` 이 가상환경(`.venv`) 생성 → 라이브러리 설치 → 설정 파일 생성까지 한 번에 합니다.

### Linux / macOS

```bash
git clone <이 저장소> auto_crawling_poc
cd auto_crawling_poc
bash scripts/linux/setup.sh
```

### 수동 설치

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install -r requirements.txt
python bot.py init
```

---

## 5분 시작하기

**1) 수집할 홍보사이트를 등록합니다.** 방법은 두 가지입니다.

```bash
# 방법 A: 명령으로 하나씩 추가
python bot.py add-source "https://홍보사이트주소/" --name "사이트 이름"

# 방법 B: config/targets.yaml 에 여러 개 적고 한 번에 반영
#   sources:
#     - url: "https://홍보사이트주소/"
#       name: "사이트 이름"
#       enabled: true
python bot.py import-targets
```

**2) 등록이 잘 됐는지 확인합니다.**

```bash
python bot.py list-sources
```

**3) 실제로 무엇이 잡히는지 미리 봅니다.** (저장하지 않고 결과만 출력)

```bash
python bot.py preview "https://홍보사이트주소/"
```

**4) 봇을 실행합니다.**

```bash
python bot.py start              # 이 창에서 실행 (Ctrl+C 로 종료)
python bot.py start --detach     # 백그라운드로 실행
```

**5) 결과를 확인합니다.**

- 엑셀: `data/exports/불법사이트_URL목록.xlsx`
- 관리 화면: <http://127.0.0.1:8787/>
- 상태: `python bot.py status`

---

## 봇 ON/OFF

**두 가지 층위**가 있습니다. 헷갈리기 쉬우니 구분해서 쓰세요.

| | 명령 | 프로세스 | 수집 | 언제 쓰나 |
|---|---|---|---|---|
| **수집 ON/OFF** | `bot.py on` / `bot.py off` | 계속 실행 | 재개 / 정지 | 평소 켜고 끄기. 즉시 반응하고, 다시 켜면 바로 한 바퀴 돕니다 |
| **프로세스 시작/종료** | `bot.py start` / `bot.py stop` | 실행 / 종료 | – | PC 재부팅, 업데이트, 완전 종료 |

`off` 는 프로세스를 살려둔 채 수집만 멈춥니다. 진행 중인 사이클은 끝까지 마무리한 뒤 멈추고,
관리 화면과 상태 조회는 계속 동작합니다.

### ON/OFF 하는 세 가지 방법

```bash
# 1) 명령줄
python bot.py off        # 수집 정지  (= pause)
python bot.py on         # 수집 재개  (= resume)
python bot.py status     # 지금 상태 확인
```

**2) 관리 화면** — <http://127.0.0.1:8787/> 에서 `수집 ON` / `수집 OFF` 버튼을 누릅니다.
현황·최근 발견 목록도 5초마다 자동 갱신됩니다.

**3) Windows 배치 파일** — 탐색기에서 더블클릭

| 파일 | 하는 일 |
|---|---|
| `scripts\windows\start-bot.bat` | 봇 실행 (창 유지) |
| `scripts\windows\start-bot-background.bat` | 봇 백그라운드 실행 |
| `scripts\windows\bot-on.bat` / `bot-off.bat` | 수집 ON / OFF |
| `scripts\windows\status.bat` | 상태 보기 |
| `scripts\windows\stop-bot.bat` | 프로세스 종료 |
| `scripts\windows\run-once.bat` | 지금 한 바퀴만 수집 |
| `scripts\windows\export.bat` | 엑셀 다시 만들기 |
| `scripts\windows\open-dashboard.bat` | 관리 화면 열기 |
| `scripts\windows\open-excel.bat` | 결과 엑셀 열기 |

ON/OFF 상태는 `run/control.json` 에 저장되므로 **봇을 껐다 켜도 유지**됩니다.

---

## 24시간 운영

### Windows — 작업 스케줄러 등록 (권장)

```powershell
# 관리자 PowerShell
cd auto_crawling_poc\scripts\windows
.\install-startup-task.ps1              # 로그온할 때 자동 시작
.\install-startup-task.ps1 -AtStartup   # 로그인 없이 부팅 시 시작 (계정 비밀번호 입력)
.\install-startup-task.ps1 -Remove      # 등록 해제
```

작업이 죽으면 **1분 뒤 자동 재시작**하고, 실행 시간 제한 없이 계속 돌도록 등록됩니다.

### Linux — systemd

`scripts/linux/illegal-site-bot.service` 의 경로/계정을 수정한 뒤:

```bash
sudo cp scripts/linux/illegal-site-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now illegal-site-bot
```

### 장시간 운영에 대비된 동작

- 한 홍보사이트에서 오류가 나도 나머지는 계속 수집하고, 한 사이클이 실패해도 다음 사이클로 넘어갑니다.
- 로그는 날짜별로 회전하고 `logging.retention_days` 일이 지나면 삭제됩니다.
- 오래된 발견 이력은 `storage.observation_retention_days` 기준으로 하루에 한 번 정리합니다
  (사이트 목록 자체는 지우지 않습니다).
- 응답 크기 상한(`max_response_bytes`)이 있어 거대한 페이지로 메모리가 늘어나지 않습니다.
- 사이클 간격에 무작위 지연(`jitter_seconds`)을 더해 접속 패턴이 기계적으로 일정해지지 않게 합니다.

---

## 엑셀 결과물

`data/exports/불법사이트_URL목록.xlsx` (+ 날짜별 스냅샷 `..._20260910.xlsx`)

| 시트 | 내용 |
|---|---|
| **요약** | 총계, 최근 24시간/7일 신규, 생존 현황, 카테고리별 집계, 마지막 사이클 정보 |
| **불법사이트목록** | 핵심 시트. URL·도메인·카테고리·위험도·점수·최초/최근 발견·발견된 홍보사이트·일치 키워드·판별 근거·생존 여부·처리 상태 |
| **신규_최근24시간** | 지난 24시간에 처음 발견된 것만 (매일 확인용) |
| **연락채널** | 텔레그램·카톡 오픈채팅 등 홍보에 쓰인 접촉 채널 |
| **홍보사이트_수집원** | 어디를 돌고 있는지, 마지막 수집 결과, 연속 실패 횟수 |
| **실행이력** | 사이클별 통계 |

몇 가지 설계 의도:

- **URL 은 기본적으로 일반 텍스트**입니다. 클릭 실수로 불법사이트에 접속하는 것을 막기 위함입니다.
  하이퍼링크가 필요하면 `export.clickable_links: true` 로 바꾸세요.
- **엑셀은 매 사이클마다 새로 생성**됩니다. 엑셀에 직접 적은 메모는 사라지므로, 처리 상태와
  메모는 DB 에 남기세요. 그러면 다음 출력에도 계속 따라옵니다.

  ```bash
  python bot.py mark "https://불법사이트주소/" --status reported --memo "2026-09-10 방심위 신고"
  # --status: new | confirmed | reported | ignored
  ```
- 엑셀 파일을 열어둔 상태로 출력 시점이 겹치면 덮어쓰지 못하므로,
  `..._열려있어_임시저장_HHMMSS.xlsx` 로 저장하고 로그에 경고를 남깁니다.

---

## 명령어 정리

| 명령 | 설명 |
|---|---|
| `bot.py init` | 설정 파일·폴더 준비 (처음 한 번) |
| `bot.py start [--detach] [--no-dashboard]` | 봇 실행 |
| `bot.py stop [--timeout 60]` | 봇 정상 종료 |
| `bot.py on` / `off` (= `resume` / `pause`) | 수집 ON / OFF |
| `bot.py status [--json]` | 상태·현황·마지막 사이클 요약 |
| `bot.py run-once` | 지금 한 바퀴만 수집 (봇이 돌고 있으면 그 봇에 요청) |
| `bot.py export [--local]` | 엑셀 다시 만들기 |
| `bot.py add-source URL [--name] [--render] [--max-pages N] [--disabled]` | 홍보사이트 추가 |
| `bot.py list-sources` | 홍보사이트 목록 |
| `bot.py enable-source URL` / `disable-source URL` | 개별 홍보사이트 켜기/끄기 |
| `bot.py remove-source URL` | 홍보사이트 삭제 |
| `bot.py import-targets` | `config/targets.yaml` 을 DB 에 반영 |
| `bot.py mark URL --status ... --memo ...` | 처리 상태·메모 기록 |
| `bot.py preview URL [--all]` | 저장하지 않고 추출·판별 결과만 확인 |

---

## 설정

`config/config.yaml` (없으면 내장 기본값으로 동작). 자주 만지는 항목만 추렸습니다.

| 항목 | 기본값 | 설명 |
|---|---|---|
| `crawl.interval_seconds` | 3600 | 한 바퀴 끝난 뒤 다음 바퀴까지 대기(초) |
| `crawl.concurrency` | 4 | 동시에 수집할 홍보사이트 수 |
| `crawl.per_host_delay_seconds` | 2.0 | 같은 호스트 연속 요청 최소 간격 |
| `crawl.max_pages_per_source` | 3 | 한 사이트에서 따라 들어갈 내부 페이지 수 |
| `crawl.respect_robots` | true | `robots.txt` 준수 |
| `crawl.resolve_candidate_redirects` | true | `/go?url=...` 같은 경유 링크의 최종 목적지 추적 |
| `renderer.enabled` | false | 자바스크립트로 배너를 그리는 사이트용 (아래 참고) |
| `alive_check.every_cycles` | 6 | 몇 사이클마다 생존 확인할지 |
| `export.min_score` | 30 | 엑셀에 담을 최소 점수 |
| `export.clickable_links` | false | 엑셀 URL 을 클릭 가능하게 |
| `dashboard.port` | 8787 | 관리 화면 포트 |
| `runtime.start_enabled` | true | 봇을 처음 켰을 때 수집 ON 으로 시작할지 |

### 자바스크립트로 배너를 그리는 홍보사이트

HTML 에 링크가 없고 스크립트로 배너를 만드는 사이트는 브라우저 렌더링이 필요합니다.

```bash
pip install playwright
playwright install chromium
```

`config.yaml` 에서 `renderer.enabled: true` 로 바꾸고, 해당 사이트만
`add-source --render` (또는 `targets.yaml` 의 `render: true`) 로 지정하세요.
전부 렌더링하려면 `renderer.render_all: true` 로 하되, 느리고 무겁습니다.

---

## 판별 규칙 다듬기 (오탐/미탐 줄이기)

판별 근거는 전부 **`config/rules.yaml`** 에 있습니다. 코드를 고칠 필요가 없습니다.

```
최종점수 = 기본점수(15)
         + 링크 텍스트/배너 alt 의 키워드 점수
         + 링크 주변 설명의 키워드 점수 (0.35배)
         + 도메인 형태 점수 (예: casino/toto 포함, 숫자 조합)
         + 의심 TLD 점수 (.xyz .top .vip .bet ...)
         + 여러 홍보사이트에서 중복 발견 보너스 (곳당 +6, 최대 +24)
```

카테고리는 **링크 자체 텍스트**를 우선해서 정합니다. 옆 배너의 키워드가 섞여 엉뚱한 분류가
되지 않도록, 링크가 여러 개 들어있는 배너 목록은 문맥으로 쓰지 않습니다.

작업 순서는 이렇게 하는 것이 빠릅니다.

```bash
# 1) 실제 페이지에서 무엇이 어떤 점수로 잡히는지 본다
python bot.py preview "https://홍보사이트주소/" --all

# 2) rules.yaml 을 고친다
#    - 놓친 업체 → categories 에 키워드 추가, 또는 domain_patterns 추가
#    - 엉뚱하게 잡힌 것 → exclude.domains / exclude.url_patterns 에 추가
#    - 후보가 너무 적다/많다 → thresholds.candidate 조정

# 3) 다시 preview 로 확인 → 반영
python bot.py run-once
```

| 증상 | 손볼 곳 |
|---|---|
| 정상 사이트가 목록에 들어옴 | `exclude.domains`, `exclude.domain_suffixes` |
| 이미지·스크립트 URL 이 들어옴 | `exclude.url_patterns` |
| 확실한 불법사이트가 빠짐 | `categories.*.keywords` 에 키워드 추가 / `thresholds.candidate` 낮추기 |
| 카테고리가 엉뚱함 | 해당 카테고리 키워드 가중치 조정 |
| 미분류가 너무 많음 | `domain_patterns`, `suspicious_tlds` 보강 |

---

## 프로젝트 구조

```
auto_crawling_poc/
├── bot.py                        실행 진입점 (python bot.py ...)
├── config/
│   ├── config.example.yaml       설정 예시 → config.yaml 로 복사해 사용
│   ├── targets.example.yaml      수집 대상 예시 → targets.yaml 로 복사해 사용
│   └── rules.yaml                판별 규칙 (키워드·도메인·제외 목록)
├── src/illegal_site_bot/
│   ├── cli.py                    명령줄 인터페이스
│   ├── daemon.py                 24시간 루프 (ON/OFF·스케줄·복구)
│   ├── control.py                ON/OFF·PID·요청 플래그·하트비트
│   ├── dashboard.py              로컬 웹 관리 화면
│   ├── pipeline.py               한 사이클 처리 흐름
│   ├── fetcher.py                HTTP 수집 (레이트리밋·robots·인코딩)
│   ├── renderer.py               (선택) Playwright 렌더링
│   ├── extractor.py              HTML → 링크 후보 추출
│   ├── normalizer.py             URL 정규화·난독화 해제·도메인 추출
│   ├── classifier.py             규칙 적용 → 점수·카테고리
│   ├── storage.py                SQLite 저장
│   ├── exporter.py               엑셀 출력
│   ├── targets.py                targets.yaml 로딩
│   ├── timeutil.py               시간 처리 (저장 UTC / 표시 로컬)
│   └── logging_setup.py          로그 설정
├── scripts/windows/              설치·실행·ON/OFF 배치 + 작업 스케줄러 등록
├── scripts/linux/                설치 스크립트 + systemd 유닛
├── tests/                        pytest (외부 인터넷 접속 없음)
├── data/                         DB·엑셀 (git 제외)
├── logs/                         로그 (git 제외)
└── run/                          ON/OFF 상태·PID·하트비트 (git 제외)
```

`config/config.yaml` 과 `config/targets.yaml` 은 **git 에 올라가지 않습니다.**
`targets.yaml` 에는 실제 수집 대상 주소가 들어가므로 의도적으로 제외했습니다.

---

## 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `이미 봇이 실행 중입니다` | 다른 프로세스가 돌고 있습니다. `bot.py status` 로 확인 후 `bot.py stop` |
| 봇을 껐는데 상태가 `실행 중` | 비정상 종료로 PID 파일이 남은 경우입니다. `bot.py status` 를 한 번 실행하면 자동 정리됩니다 |
| 관리 화면이 안 열림 | 포트 충돌입니다. `dashboard.port` 를 바꾸거나 `--no-dashboard` 로 실행 |
| 엑셀이 갱신되지 않음 | 파일을 열어둔 상태였을 수 있습니다. 로그 확인 후 엑셀을 닫고 `bot.py export --local` |
| 특정 사이트만 계속 실패 | `bot.py list-sources` 의 연속 실패 확인 → `bot.py preview URL` 로 원인 확인. 차단·폐쇄되었으면 `disable-source` |
| 한글이 깨져 보임 | 봇은 EUC-KR/CP949 를 자동 판별합니다. Windows 콘솔이라면 `chcp 65001` |
| `robots.txt 로 건너뜀` 로그 | 대상이 수집을 금지하고 있습니다. 수집 근거를 검토한 뒤에만 `crawl.respect_robots` 를 조정하세요 |
| 링크가 하나도 안 잡힘 | 스크립트로 배너를 그리는 사이트일 수 있습니다. 위 "자바스크립트" 항목 참고 |
| 결과가 너무 적음/많음 | `export.min_score`, `thresholds.candidate` 조정 |

로그: `logs/bot.log` (날짜별 회전). 자세히 보려면 `logging.level: DEBUG`.

---

## 데이터와 보안

- 수집 데이터는 **이 PC 안에만** 저장됩니다 (`data/collector.sqlite3`, `data/exports/`). 외부로 전송하지 않습니다.
- 관리 화면은 기본적으로 `127.0.0.1` 에만 바인딩되어 이 PC 에서만 열립니다.
  다른 PC 에서 접속하려면 `dashboard.host` 를 바꿔야 하는데, 그 경우 `dashboard.token` 설정이
  강제됩니다(설정 검증에서 막힘). 사내망이라도 노출은 최소화하세요.
- 백업이 필요하면 `data/` 폴더만 복사하면 됩니다.

## 개발

```bash
python -m pytest              # 전체 테스트 (외부 인터넷 접속 없이 로컬 가짜 서버로 검증)
python -m pytest -k extractor
```
