# 코덱스 리셋 웹 감시 봇

Oracle Linux 서버로 이전하는 절차와 설치·자동 실행 파일은 [deploy/ORACLE.md](deploy/ORACLE.md)에 있습니다.

[@thsottiaux](https://x.com/thsottiaux)의 공개 프로필 웹페이지를 5분마다 읽고, 새 글을 **ChatGPT 계정으로 로그인한 Codex CLI**로 해석합니다. 리셋 예고·변경·취소·완료 주장 등 관련 글만 텔레그램으로 보냅니다. X API·Bearer Token·유료 X 크레딧을 사용하지 않습니다. AI 분석은 로그인한 Codex의 이용 한도를 사용합니다.

## 준비

- Python 3.10 이상, 인터넷 연결, 계속 켜져 있는 PC 또는 서버.
- Codex CLI 설치 및 `codex login`으로 ChatGPT 계정 로그인. `codex login status`에서 `Logged in using ChatGPT` 확인. Chrome/ChatGPT 앱 로그인과 CLI 인증은 별개입니다. 현재 이 PC의 CLI 로그인은 검증했습니다.
- 텔레그램 [@BotFather](https://t.me/BotFather)의 `/newbot`으로 생성한 봇 토큰.

## Windows 설정

프로젝트 폴더의 PowerShell에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

`.env`에 `TELEGRAM_BOT_TOKEN`을 입력합니다. 기존 `.env`에 `X_BEARER_TOKEN`이 남아 있어도 무시합니다. 토큰은 채팅이나 Git에 올리지 마세요. `X_USERNAME=thsottiaux`가 기본 대상입니다. 기존 설치도 의존성 설치 명령을 다시 실행하세요.

텔레그램에서 생성한 봇과 대화를 열고 `/start`를 보낸 뒤 다음 명령을 실행합니다. BotFather가 아닌 만든 봇에게 보내세요.

```powershell
.\.venv\Scripts\python.exe -B bot.py --setup-telegram
```

개인 채팅이 하나이면 테스트 메시지를 보내고 `TELEGRAM_CHAT_ID`를 `.env`에 저장합니다. 개인 채팅이 없으면 올바른 봇에 `/start`를 보내세요. 여러 개이면 자동 선택하지 않습니다. `--chat-id`로 ID를 확인해 `.env`에 직접 입력하세요. `getUpdates`는 기존 webhook과 함께 사용할 수 없으므로 이 프로젝트 전용 봇을 권장합니다.

### 감시 계정

`.env`의 `X_ACCOUNTS`에 `계정:제품` 형식으로 쉼표로 구분해 넣습니다. 제품은 `codex` 또는 `claude`이며, Codex AI가 그 제품의 사용량 초기화 소식인지 판단하고 알림 제목에도 제품 이름을 붙입니다.

```
X_ACCOUNTS=thsottiaux:codex,claudedevs:claude
```

`X_ACCOUNTS`가 비어 있으면 기존 `X_USERNAME`을 Codex 계정으로 감시합니다. 새로 추가한 계정은 처음 실행 시 `INITIAL_LOOKBACK_HOURS` 이내의 글만 확인합니다. 한 계정의 페이지 오류는 다른 계정 감시를 막지 않습니다.

### 디스코드 (선택)

디스코드 채널 설정 → 연동 → 웹후크에서 만든 URL을 `.env`의 `DISCORD_WEBHOOK_URL`에 넣습니다. 텔레그램과 디스코드가 둘 다 설정되면 두 곳 모두로 보내고, 디스코드만 쓰려면 `TELEGRAM_BOT_TOKEN`을 비워 두세요. 웹훅 URL도 비밀번호처럼 관리하세요. 2000자를 넘는 알림은 여러 메시지로 나누어 보내며, `@everyone` 같은 멘션은 울리지 않습니다.

```powershell
.\.venv\Scripts\python.exe bot.py --test-discord
```

```powershell
# 실제 본인 채팅으로 연결 확인 메시지 전송
.\.venv\Scripts\python.exe bot.py --test-telegram
# 실제 X 웹 조회 + Codex 해석. 알림은 콘솔 출력 (Telegram 전송·봇 상태 저장 없음)
.\.venv\Scripts\python.exe bot.py --dry-run
# 상시 감시
.\run.ps1
# 기존 공개 글 하나의 새 AI 알림 양식 미리보기 (Codex 한도 사용)
.\.venv\Scripts\python.exe -B bot.py --preview-ai
```

`--once`는 한 번 조회하고 관련 새 글을 실제 전송한 뒤 종료합니다. 인터넷 없이 화면 예시를 보려면 `python bot.py --sample`을 사용합니다. 예시의 게시글과 링크 ID 0은 테스트용입니다. 콘솔의 `공개 글 N개 / 새로 처리 M개`에서 N은 현재 HTML에 보이는 글 수이며 전체 글 수가 아닙니다. M에는 키워드가 없는 글도 포함됩니다.

## AI 해석과 알림 양식

```text
원본 메시지 :
게시글 원문

AI 해석 :
한국어 번역과 의미 설명, 실제 예고인지/농담인지/변경인지 구분

예정 시간 : 한국 시간 또는 미정
판단 확신도 : 높음/보통/낮음 (AI 판단)
근거 : 실제 원문 인용
확인할 점 : 시간대·날짜·맥락의 불확실성
게시 시각 / 작성자 / 원문 링크
```

- 기본 `AI_ENABLED=true`. 새 글은 reset 키워드가 없어도 분석합니다. 관련 없는 글은 기록만 하고 알리지 않습니다. 기존 처리 ID는 유지하므로 업데이트 전 처리한 글을 일괄 재전송하지 않습니다.
- 같은 계정의 이전 72시간 글 최대 8개를 맥락으로 제공합니다. 이웃한 글이 곧 답글 관계는 아니며, 관계가 확인되지 않은 `Done`으로 완료를 단정하지 않습니다. 공개 페이지가 제공하지 않는 대화·이미지·숨겨진 글은 이해할 수 없습니다.
- 원문에 없는 인용·잘못된 JSON·근거 없는 시각은 거부합니다. 상대시간의 분 수와 명시적 날짜·시간대 계산값을 원문 표현과 코드로 대조하고, 결과를 한국 시간으로 변환합니다. 지원하는 표현은 `in 3 hours`, `in three hours`, `in 1 hour and 30 minutes`, 시간대 포함 ISO 일시, `YYYY-MM-DD` 또는 `today`/`tomorrow`와 시간대가 있는 숫자 시각입니다. 그 밖의 표현은 AI에 시간 미정으로 반환하도록 지시하며, 검증을 통과하지 못한 결과는 보관하고 재시도합니다. AI의 문맥 오해까지 완전히 막지는 못하므로 원문도 확인하세요.
- CLI는 읽기 전용, 셸·웹 검색·앱/플러그인 도구 비활성화, 전용 빈 작업 폴더에서 실행합니다. 게시글은 명령이 아닌 데이터로 전달합니다. 봇 비밀키와 Dochi 라우팅 환경은 자식 프로세스에 넘기지 않습니다. 기존 CLI 로그인은 재사용합니다.
- `CODEX_MODEL` 공란은 CLI 기본 모델. `CODEX_COMMAND`는 필요할 때만 codex.exe 절대 경로. `CODEX_TIMEOUT_SECONDS` 기본 120초, 10~600초. `AI_MAX_POSTS_PER_POLL` 기본 10개, 1~50개. AI 오류 시 규칙 기반 결과로 조용히 대체하지 않습니다.
- 읽어 둔 글은 보류 큐에 저장하고 AI 오류·시간 초과 시 완료 처리하지 않습니다. 실패 횟수와 다음 재시도 시각을 글별로 저장하며, 실패한 글 뒤의 새 글은 계속 처리합니다. 한 주기의 분석 개수 제한에는 실패한 시도도 포함됩니다. 검증한 해석은 SQLite에 캐시하며 맥락·모델·분석 지침·스키마가 바뀌면 다시 분석합니다. 이미 전송을 시작한 알림은 당시 본문과 분석을 고정해 재시작 후에도 이어 보냅니다. 새 글이나 후속 변경이 없을 때는 모델을 호출하지 않습니다. `--notify-startup`은 시작 시 별도 연결 시험을 수행합니다.
- `AI_ENABLED=false`로 명시하면 이전 규칙 모드로 실행할 수 있습니다. AI 실패 시에는 로그인 상태·이용 한도를 확인하세요.

## 자동 실행

연결 확인 후 직접 실행하세요. 로그인 시 동작하는 Windows 예약 작업을 등록합니다. 동일 작업이 실행 중이면 새 인스턴스를 시작하지 않습니다.

```powershell
.\install-task.ps1
Start-ScheduledTask -TaskName CodexResetAlertBot
Get-ScheduledTaskInfo -TaskName CodexResetAlertBot
# 중지/제거
Stop-ScheduledTask -TaskName CodexResetAlertBot
Unregister-ScheduledTask -TaskName CodexResetAlertBot -Confirm:$false
```

예약 작업은 `--notify-startup`을 사용합니다. 시작 후 실제 X 조회·Codex 연결 시험·미처리 글 처리가 성공해야 재시작 성공 메시지를 보냅니다. 콘솔 수동 실행도 `.\run.ps1 --notify-startup`으로 같은 확인을 사용할 수 있습니다.

수동 실행과 예약 작업을 동시에 실행하지 마세요. PC 종료·절전·로그아웃 중에는 감시되지 않습니다. 24시간 운영하려면 항상 켜진 서버에서 동일 프로그램을 서비스로 실행해야 합니다. 이 저장소만 생성한 상태에서는 예약 작업이나 실제 감시가 자동 시작되지 않습니다.

상시 감시 로그는 상태 DB와 같은 폴더의 `bot.log`에 기록합니다 (기본 `data/bot.log`, 최대 500KB씩 최근 3개 파일 유지). 예약 작업으로 실행할 때도 이 파일에서 마지막 확인 시각과 오류를 확인할 수 있습니다.

## 판독과 보관 방식

- `in 3 hours`: **게시 시각**에 3시간을 더해 계산합니다.
- `tomorrow at 10am PT`, `2026-10-20 at 10:30 UTC`, 시간대 포함 ISO 일시: 명시된 시간대를 기준으로 한국 시간 변환. PT/ET는 서머타임 반영.
- `tomorrow morning`, 시간대 없는 시각, 날짜 없는 `10am PT`, 서머타임 전환의 모호한 시각: **시간 미정/확인 필요**로 원문 전송. 임의로 한국 시간 일시를 만들지 않습니다.
- 취소·완료·질문·가능성·농담을 AI가 구분합니다. AI도 오해할 수 있고 이미지 속 시간·대화 맥락·수정된 기존 글을 완전하게 판독하지 못합니다. 원문을 확인하세요.
- **공개 페이지에 실제로 보이는 글만 확인합니다.** 로그아웃 페이지는 일부 글만 제공하거나 순서를 바꿀 수 있습니다. 모든 새 글·답글 수집이나 장기 중단 후 복구를 보장하지 않습니다. 유료 API로 자동 전환하지 않습니다.
- 본문·링크를 HTML에서 읽습니다. 게시 시각은 time 요소, 페이지 내 숫자 timestamp, 없으면 게시글 ID 생성 시각 순서로 판독합니다. 페이지 스크립트를 실행하지 않습니다.
- 첫 실행은 최근 24시간(`INITIAL_LOOKBACK_HOURS`)의 보이는 글을 확인합니다. 시작 기준 시각과 처리한 글 ID를 SQLite(`STATE_PATH`)에 저장합니다. 시작 기준은 재시작 후에도 유지됩니다.
- **글별 처리 여부**로 고정 글·순서 변경의 중복을 막습니다. 나중에 보이는 글도 시작 기준 이후이고 미처리이면 확인합니다. 채널·메시지 조각별 전송 성공을 SQLite에 저장해 부분 실패·재시작 때 성공한 부분은 건너뜁니다. 외부 서버에서 메시지를 수락한 직후 성공 기록 전에 중단되거나, 서버가 수락했지만 응답이 유실되면 중복이 생길 수 있습니다.
- 상세 페이지에서 작성자와 게시 시각이 가깝다는 이유만으로 답글로 연결하지 않습니다. HTML의 명시적인 부모 ID 메타데이터로 원글까지 연결되는 작성자 답글만 제공합니다. X가 관계 메타데이터를 제공하지 않으면 해당 답글은 분석에 포함되지 않습니다.
- 예고·일정 변경·불확실한 관련 글은 게시 후 최소 6시간 동안 원문 수정과 확인된 후속 답글을 감시합니다. 예정 시간이 있으면 그 시각 2시간 뒤까지 연장하되 게시 후 최대 7일입니다. 시간이 확인돼도 변경·취소 확인을 계속하며 완료·취소로 판정되면 종료합니다. 실패한 후속 처리는 만료 시각 이후에도 보관하고 재시도합니다.
- 로그인 요구·본문 구조 변경·접근 차단은 ‘새 글 없음’이 아닌 감시 오류로 처리합니다. 설정된 채널에 장애·복구를 알리며 상태는 재시작 후에도 유지됩니다. 미복구 상태는 `HEALTH_REPEAT_MINUTES` 간격(기본 60분)으로 다시 알립니다. 알림 채널 자체의 장애는 로그와 `--status`로 확인하세요.
- HTTP 429·일시 오류는 대기 후 재시도합니다. 기본 주기는 300초, `POLL_SECONDS` 최소 60초입니다. 조회 오류의 대기 시간은 계정별로 늘어나며 정상 계정의 주기를 함께 늘리지 않습니다. 전송 채널의 429 대기 시간도 저장합니다. HTTP 연결을 재사용하고 같은 상세 페이지는 45초간 재사용합니다. AI 분석은 순차 실행하므로 긴 분석은 다음 조회나 재알림을 늦출 수 있습니다.
- 기존 상태 DB는 봇 시작 시 필요한 테이블·인덱스를 추가하며 처리 ID와 시작 기준은 유지합니다. 기본 90일(`RETENTION_DAYS`, 최소 7일)이 지난 처리 완료 본문·분석·완료 전송 이력은 하루 한 번 정리합니다. 미처리 글·감시 중인 글·미완료 전송과 처리 ID는 보관합니다. SQLite 파일의 디스크 용량은 삭제 직후 자동 축소되지 않습니다.

## 상태·이력 조회와 재알림

다음 명령은 인증 정보 없이 로컬 상태를 읽으며, 네트워크 요청이나 상태 DB 생성·변경을 하지 않습니다. `--status`는 계정별 마지막 조회·AI 성공, 미처리 글 수, 재시도 수와 오류를 보여 줍니다. `--history`는 업데이트 이후 기록된 알림 본문과 전송 완료·미완료·취소·만료 상태를 보여 줍니다.

```powershell
.\.venv\Scripts\python.exe -B bot.py --status
.\.venv\Scripts\python.exe -B bot.py --history
.\.venv\Scripts\python.exe -B bot.py --history 10
```

예정 시각 재알림은 기본 비활성화입니다. 필요하면 `.env`에 다음 설정을 추가하고 봇을 재시작하세요. 기존 `.env`는 자동으로 변경하지 않습니다.

```dotenv
REMINDER_MINUTES=10,0
HEALTH_REPEAT_MINUTES=60
RETENTION_DAYS=90
```

`10,0`은 10분 전과 예정 시각 도달 시 알립니다. 이는 원문 일정의 재알림이며 실제 리셋 완료 확인이 아닙니다. 검증된 변경·취소는 알림 전송에 실패해도 기존 예약부터 무효화합니다. 별도 글의 변경·취소는 부모 관계가 확인된 기존 일정에 연결될 때 반영합니다. 관계가 없는 다른 공지의 일정을 임의로 취소하지 않습니다. 재시작 후 지난 재알림을 몰아서 보내지 않으며 최대 3분의 지연·짧은 재시도만 허용합니다. `--once`는 한 번 처리하고 종료하므로 지속적인 재알림에는 상시 실행이 필요합니다. 실패로 보류 중인 작업이 있으면 `--once`는 종료 코드 1을 반환합니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -B -m unittest -v
```

테스트는 모의 HTML·HTTP·텔레그램을 사용하며 실제 메시지를 보내지 않습니다. 실서비스 연결은 위의 `--test-telegram`, `--dry-run`으로 별도 확인하세요.

참고한 공식 문서: [Telegram Bot API](https://core.telegram.org/bots/api), [Codex 자동 실행](https://learn.chatgpt.com/docs/non-interactive-mode), [Codex 인증](https://learn.chatgpt.com/docs/auth).
