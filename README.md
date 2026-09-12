# 불법사이트 URL 수집 봇

불법홍보사이트(도박·성인·불법복제 사이트를 배너로 홍보하는 페이지)를 주기적으로 돌면서,
거기서 홍보되는 **불법사이트 URL을 뽑아 엑셀로 정리**하는 봇입니다.
워크스테이션 PC 에서 24시간 상주하며, 수집을 **ON/OFF** 로 언제든 켜고 끌 수 있습니다.

```
 config/targets.yaml        ┌───────────── 봇 프로세스 (24시간 상주) ─────────────┐
 (홍보사이트 목록)   ──▶    │                                                    │
                            │  ① 페이지 수집   HTTP / (선택)브라우저 렌더링       │
                            │  ② 링크 추출     a href · 배너 img · onclick ·     │
                            │                  data-href · iframe · script ·     │
                            │                  본문 텍스트(abc[.]com 형태)        │
                            │  ③ 경유 추적     /go?url=... → 최종 도착지          │
                            │  ④ 판별/점수     rules.yaml 키워드·도메인 규칙      │
                            │  ⑤ 저장          SQLite (신규/중복/발견이력)        │
                            │  ⑥ 엑셀 출력     시트 7개                          │
                            │  ⑦ 생존 확인     아직 열려 있는지 주기 점검         │
                            │  ⑧ 자동 발견     다른 홍보사이트를 찾아 후보로      │
                            │                  등록 → 승인되면 수집 대상에 추가   │
                            └────────────────────────────────────────────────────┘
                                 ▲            │                    │
             ON/OFF · 승인/기각  │            │ 수집 대상이        ▼
             (CLI · 웹 관리화면) ┘            └─ 스스로 넓어짐   data/exports/*.xlsx
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

## 홍보사이트 자동 발견 (수집 범위 넓히기)

사람이 넣은 홍보사이트만 돌면 수집 범위가 고정됩니다. 봇은 수집 중에 **"또 다른
홍보사이트"로 보이는 링크**를 찾아 수집 대상 후보로 올려둡니다. 승인하면 다음
사이클부터 그곳까지 돌기 시작합니다.

```
시드(수동 등록, 깊이 0) ──발견──▶ 후보 ──평가──▶ 승인 대기 ──사람 승인──▶ 수집 대상(깊이 1)
                                        └─기준 미달─▶ 기각            └──발견──▶ 깊이 2
```

### 어떻게 홍보사이트인지 알아내나

두 단계로 판별합니다. 1단계는 요청 없이 공짜고, 2단계만 후보당 HTTP 요청을 1회 씁니다.

**1단계 (후보 등록)** — 링크 텍스트나 주변 설명에 `먹튀검증` `보증업체` `링크모음`
`제휴` 같은 홍보사이트 어휘가 있을 때만 후보로 올립니다. 이 관문이 없으면 후보가
폭증합니다. 어휘 목록은 `rules.yaml` 의 `promotion.link_vocabulary` 입니다.

**2단계 (평가)** — 후보 페이지를 실제로 받아 점수를 냅니다.

| 신호 | 배점 | 설명 |
|---|---|---|
| **이미 아는 불법 도메인으로 나가는 링크 수** | **최대 50** | **가장 결정적.** 불법사이트 본체는 경쟁 업체를 링크하지 않습니다 |
| 외부 도메인 총 개수 | 최대 15 | 단독으로는 기준을 넘지 못하도록 낮게 배점 (뉴스 사이트 오탐 방지) |
| 본문의 도박·성인 키워드 | 최대 15 | `categories` 사전 재사용 |
| 제목·헤딩의 홍보 어휘 | 최대 20 | 자기 정체성을 드러내는 것이라 오탐이 적음 |
| 이미지 배너 비율 | 최대 10 | 홍보사이트의 전형적 형태 |
| 로그인 위주 + 외부 링크 거의 없음 | **-30** | 홍보사이트가 아니라 **불법사이트 본체** |

첫 번째 신호가 핵심입니다. 그리고 **수집한 불법사이트가 쌓일수록 이 판별이
정확해집니다** — 불법사이트를 많이 알수록 홍보사이트를 더 잘 찾고, 홍보사이트가
늘면 불법사이트를 더 찾는 선순환입니다.

> **초기에는 판별력이 약합니다.** DB 가 비어 있으면 이 신호가 0점이라 오탐이 늘어납니다.
> 그래서 기본값이 `auto_approve: false` 입니다. 수집된 불법사이트가 100건을 넘어가면
> 자동 승인을 검토하세요.

### 후보 확인하고 승인하기

```bash
python bot.py candidates                      # 승인 대기 목록 (점수·근거·발견 경로)
python bot.py approve "https://후보주소/"      # 승인 → 다음 사이클부터 수집
python bot.py reject  "https://후보주소/"      # 기각 (30일 뒤 재평가)
python bot.py evaluate "https://주소/"         # 저장하지 않고 점수만 계산
python bot.py evaluate "https://주소/" --add   # 점수가 기준을 넘으면 후보로 추가
```

관리 화면(<http://127.0.0.1:8787/>)에도 **승인 대기 후보 카드**가 뜨고, 버튼으로
승인/기각할 수 있습니다. 엑셀의 `홍보사이트_후보` 시트에서도 같은 내용을 봅니다.

### 탐색 깊이

`max_depth: 2` 는 **시드 → 발견1 → 발견2** 까지만 간다는 뜻입니다. 깊이 2 사이트에서
발견된 링크는 평가조차 하지 않습니다.

분기 계수를 5로 잡고 시드 5개로 시작하면 깊이 2 에서 누적 약 155곳(사이클 3~5분),
깊이 3 이면 780곳(15~25분)이 됩니다. 깊이 3 부터는 처음 시드와의 연관성도 옅어집니다.

깊이별 성과는 엑셀 `홍보사이트_수집원` 시트의 `깊이` 와 `누적 수집` 으로 확인할 수
있습니다. 깊이 2 의 수집 성과가 거의 없으면 `max_depth: 1` 로 낮추세요.

깊이 2 에서 발견된 곳이 중요해 보이면 `add-source` 로 직접 등록하면 됩니다.
그러면 **깊이 0 시드로 승격**되어 거기서 다시 2홉이 뻗어나갑니다.

### 폭주 방지 장치

자가 확장은 그대로 두면 기하급수적으로 늘어납니다. 모든 방향에 상한이 있습니다.

| 설정 | 기본값 | 막는 것 |
|---|---|---|
| `max_depth` | 2 | 주제에서 멀어지는 것 |
| `max_total_sources` | 500 | 수집 대상이 무한정 늘어나는 것 |
| `max_candidates_per_cycle` | 50 | 한 사이클에 후보가 쏟아지는 것 |
| `max_new_per_cycle` | 5 | 자동 승인이 한꺼번에 일어나는 것 |
| `max_evaluations_per_cycle` | 20 | 평가용 요청이 늘어나는 것 |
| `auto_disable_after_failures` | 10 | 죽은 사이트가 쌓여 사이클이 느려지는 것 |
| `mirror_similarity` | 0.8 | 같은 사이트의 복제본을 중복 수집하는 것 |

이미 등록된 도메인, 제외 목록에 있는 도메인, 연락 채널은 후보가 되지 않습니다.

### 알아두면 좋은 동작

- **홍보사이트는 불법사이트로도 잡힙니다.** 도박 키워드를 잔뜩 달고 있기 때문입니다.
  2단계에서 홍보사이트로 확정되면 불법사이트 목록에서 자동으로 빠지고(`제외` 처리),
  수집원 목록으로 옮겨집니다.
- **후보는 메인 페이지로 등록됩니다.** `https://promo.com/board/read?id=3` 로 발견해도
  `https://promo.com/` 을 등록합니다. 배너 전체를 봐야 하기 때문입니다.
- 자동 발견을 아예 끄려면 `discovery.enabled: false` 로 두세요. 그러면 사람이 등록한
  홍보사이트만 돕니다.

---

## 엑셀 결과물

`data/exports/불법사이트_URL목록.xlsx` (+ 날짜별 스냅샷 `..._20260910.xlsx`)

| 시트 | 내용 |
|---|---|
| **요약** | 총계, 최근 24시간/7일 신규, 생존 현황, 카테고리별 집계, 마지막 사이클 정보 |
| **불법사이트목록** | 핵심 시트. **호스트 단위로 묶어서** 한 줄씩. 대표 URL·발견된 URL 전체·도메인·카테고리·위험도·점수·발견된 홍보사이트·판별 근거·생존 여부·처리 상태 |
| **신규_최근24시간** | 지난 24시간에 처음 발견된 것만 (매일 확인용) |
| **연락채널** | 텔레그램·카톡 오픈채팅 등 홍보에 쓰인 접촉 채널 |
| **홍보사이트_수집원** | 어디를 돌고 있는지, 마지막 수집 결과, 연속 실패 횟수, 수동/자동 구분과 깊이 |
| **홍보사이트_후보** | 봇이 새로 찾아낸 홍보사이트 (승인 대기 / 기각), 점수와 판별 근거 |
| **실행이력** | 사이클별 통계 |

몇 가지 설계 의도:

- **같은 사이트는 호스트 단위로 묶입니다.** 홍보사이트마다 링크한 주소가 달라서
  (`abc.com/` vs `abc.com/join`) 같은 업체가 여러 줄로 나오는 것을 막습니다. 발견된 주소는
  `발견된 URL` 칸에 전부 남으므로 증거는 잃지 않습니다. 자세한 내용은 아래 "묶는 기준" 참고.
- **URL 은 기본적으로 일반 텍스트**입니다. 클릭 실수로 불법사이트에 접속하는 것을 막기 위함입니다.
  하이퍼링크가 필요하면 `export.clickable_links: true` 로 바꾸세요.
- **엑셀은 매 사이클마다 새로 생성**됩니다. 엑셀에 직접 적은 메모는 사라지므로, 처리 상태와
  메모는 DB 에 남기세요. 그러면 다음 출력에도 계속 따라옵니다.

  ```bash
  # 엑셀에서 읽은 호스트를 그대로 넣으면 그 아래 URL 이 한꺼번에 처리됩니다.
  python bot.py mark "abc-777.xyz" --status reported --memo "2026-09-10 방심위 신고"
  # --status: new | confirmed | reported | ignored
  ```
- 엑셀 파일을 열어둔 상태로 출력 시점이 겹치면 덮어쓰지 못하므로,
  `..._열려있어_임시저장_HHMMSS.xlsx` 로 저장하고 로그에 경고를 남깁니다.

### 묶는 기준 (`export.group_by`)

같은 업체가 경로만 다르게 여러 줄로 나오는 것을 막습니다. 홍보사이트 A 는
`abc.com/join` 을, B 는 `abc.com/` 을 링크하는 식이라 URL 그대로 두면 갈라집니다.

| 값 | `a.abc.com` 과 `b.abc.com` | `abc.com/` 과 `abc.com/join` | 쓰임 |
|---|---|---|---|
| `host` (기본) | 따로 | **하나로** | 심의 의뢰·관리. 서브도메인마다 다른 업체가 도는 경우를 뭉개지 않습니다 |
| `domain` | 하나로 | 하나로 | 행 수를 가장 줄이고 싶을 때 |
| `url` | 따로 | 따로 | 증거 보전 — 발견된 주소를 하나하나 남겨야 할 때 |

묶는 건 보기 좋으라고 하는 게 아닙니다. **점수가 정확해집니다.** "여러 홍보사이트에서
중복 발견되면 가산" 규칙을 URL 단위로 세면, A 와 B 가 서로 다른 경로를 링크했다는
이유로 각각 "1곳"이 되어 가산이 안 붙습니다. 묶어서 세야 실제 2곳으로 잡힙니다.

> **연락채널 시트는 묶지 않습니다.** `t.me/계정A` 와 `t.me/계정B` 는 호스트가 같아도
> 서로 다른 채널이라, 묶으면 한 줄로 뭉개집니다.


### 외부 시스템 연동 (CSV)

수집 결과를 다른 시스템(예: AI 기반 탐지 시스템)이 읽어가도록 CSV 도 함께 만듭니다.

| 파일 | 내용 |
|---|---|
| `data/exports/urls.csv` | 전체 목록 |
| `data/exports/urls_new.csv` | 최근 24시간에 처음 발견된 것만 (증분 처리용) |

컬럼은 `url`, `score`, `first_seen_at`, `last_seen_at`, `host`, `domain`, `category`,
`promo_site_count`, `promo_sites`, `alive`, `http_status`, `last_checked_at`,
`matched_keywords`, `reasons`, `status` 입니다.

- **엑셀과 달리 묶지 않고 URL 단위 그대로** 내보냅니다. 받는 쪽이 정보를 잃지 않게 하기
  위함이고, 묶고 싶으면 `host` / `domain` 컬럼으로 직접 묶으면 됩니다.
- 쓰는 중에 읽어도 깨지지 않도록 **임시 파일에 쓴 뒤 원자적으로 교체**합니다.
- UTF-8 BOM 이 붙어 있어 한글 엑셀에서 바로 열립니다.
- 연락채널과 제외 처리된 항목은 빠집니다.

`export.csv_min_score: 0` 이 기본값이라 DB 에 있는 것을 전부 넘깁니다. 뒤에서 한 번 더
거르는 시스템이 있다면 이대로 두는 것이 좋습니다.


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
| `bot.py candidates [--state ...]` | 봇이 찾아낸 홍보사이트 후보 목록 |
| `bot.py approve URL` / `reject URL` | 후보 승인 / 기각 |
| `bot.py evaluate URL [--add]` | 어떤 주소가 홍보사이트인지 점수만 계산 |

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
| `discovery.enabled` | true | 다른 홍보사이트 자동 발견 |
| `discovery.auto_approve` | false | 사람 확인 없이 바로 수집 대상에 추가 |
| `discovery.max_depth` | 2 | 시드에서 몇 홉까지 탐색할지 |
| `discovery.max_total_sources` | 500 | 홍보사이트 전체 상한 |
| `export.min_score` | 30 | 엑셀에 담을 최소 점수 |
| `export.group_by` | host | 엑셀에서 같은 사이트를 묶는 기준 (host/domain/url) |
| `export.csv_enabled` | true | 외부 시스템용 CSV 동시 생성 |
| `export.csv_min_score` | 0 | CSV 에 담을 최소 점수 (0 = 전부) |
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
| 홍보사이트 후보가 안 잡힘/너무 잡힘 | `promotion.link_vocabulary` 와 `discovery.queue_score` 조정 |

---

## 프로젝트 구조

```
auto_crawling_poc/
├── bot.py                        실행 진입점 (python bot.py ...)
├── config/
│   ├── config.example.yaml       설정 예시 → config.yaml 로 복사해 사용
│   ├── targets.example.yaml      수집 대상 예시 → targets.yaml 로 복사해 사용
│   └── rules.yaml                판별 규칙 (불법사이트 + 홍보사이트 판별)
├── src/illegal_site_bot/
│   ├── cli.py                    명령줄 인터페이스
│   ├── daemon.py                 24시간 루프 (ON/OFF·스케줄·복구)
│   ├── control.py                ON/OFF·PID·요청 플래그·하트비트
│   ├── dashboard.py              로컬 웹 관리 화면
│   ├── pipeline.py               한 사이클 처리 흐름
│   ├── discovery.py              홍보사이트 자동 발견 (후보 등록·평가·승인)
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
| 후보가 하나도 안 잡힘 | 홍보사이트끼리 링크할 때 쓰는 표현을 `rules.yaml` 의 `promotion.link_vocabulary` 에 추가 |
| 엉뚱한 사이트가 후보로 올라옴 | 승인하지 말고 `reject`. 반복되면 `exclude.domains` 에 추가 |
| 수집 대상이 너무 빨리 늘어남 | `discovery.max_depth` 를 1 로 낮추거나 `max_total_sources` 를 줄이세요 |

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
