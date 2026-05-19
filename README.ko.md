# line-chrome-skill

[English](README.md) | **한국어** | [日本語](README.ja.md)

Claude Code, Codex 같은 에이전트에서 **`/line-chrome` 스킬로 LINE을 조작**하게 하는
로컬 스킬입니다. 사용자는 "김용진한테 5분 늦는다고 보내줘", "김용진 대화 요약해줘",
"3분 뒤에 모하누라고 보내줘"처럼 자연어로 요청하고, 에이전트가 이 저장소의
`SKILL.md`와 `cli.py`를 이용해 실제 LINE Chrome 확장을 조작합니다.

`cli.py`는 스킬의 실행 엔진입니다. 직접 터미널에서 쓸 수도 있지만, 기본 사용 모델은
에이전트가 `/line-chrome` 스킬을 발견하고 필요한 CLI 명령을 대신 실행하는 방식입니다.

LINE 데스크톱 앱을 건드리거나, 토큰을 빼내거나, LINE의 비공개 API를
리버스엔지니어링하지 **않습니다**. 모든 발신 메시지는 사용자 본인이 로그인한 LINE
확장이 생성하며, UI에서 직접 타이핑한 것과 동일합니다.

> macOS 전용. AppleScript ↔ Chrome 브리지에 의존합니다.

<!-- 선택: 데모 스크린샷이나 GIF를 여기에 넣으세요. 예: ![demo](docs/demo.gif) -->

## 에이전트에서 무엇을 할 수 있나요

Claude Code, Codex, 또는 `SKILL.md`를 읽는 다른 로컬 에이전트에서 `/line-chrome`을
호출하거나 LINE 작업을 자연어로 요청하면 됩니다:

| 이렇게 말하면 | 이런 일이 일어납니다 |
| --- | --- |
| "김용진한테 5분 늦는다고 보내줘" | 채팅방을 찾아 입력하고 전송 — 전달까지 검증 |
| "팀 채팅방에서 내가 놓친 거 알려줘" | 최근 메시지를 읽고 정리해 보고 |
| "가족 그룹 오늘 대화 요약해줘" | 대화 기록을 가져와 요약 |
| "어제 용진이가 보낸 주소 찾아줘" | 채팅방 메시지에서 키워드 검색 |
| "프로젝트방에 누가 답하면 알려줘" | 새 수신 메시지를 감시 |
| "3분 뒤에 김용진한테 모하누라고 보내줘" | 예약 큐에 넣고 due 시각에 전송 |
| "김용진 방에는 어떤 tone이 어울려?" | 최근 대화 흐름을 읽고 톤 프로필 추천 |

### 의미 있는 활용 시나리오

- **아침 따라잡기** — 밤사이 쌓인 여러 채팅방 메시지를 한 번에 요약.
- **답장 초안 작성** — 어시스턴트가 스레드를 읽고 맥락에 맞는 답장을 초안으로
  작성하면, 사용자가 승인 후 전송.
- **받은편지함 분류** — "내가 아직 답 안 한 직접 질문 있어?"
- **예약 리마인더** — `cron`과 결합: 평일 오전 9시마다 채팅방에 스탠드업 알림 전송.
- **아카이빙** — 채팅방 기록을 파일로 덤프해 보관.

이 모든 자연어 요청은 내부적으로 일반 CLI 서브커맨드(`send`, `brief`, `history`,
`search`, `schedule` 등)로 매핑됩니다. 즉 CLI는 사용자가 직접 쓰는 화면이라기보다,
스킬이 안정적으로 호출하는 로컬 실행 계층입니다.

## 동작 원리

```
cli.py  ──osascript──▶  Google Chrome  ──execute javascript──▶  LINE 확장 DOM
```

LINE 확장은 Chrome 탭 안에서 일반 웹페이지로 렌더링됩니다. `cli.py`는 그 탭을 찾아
작은 JS 조각을 실행합니다(검색창 설정, 채팅방 행 클릭, 에디터에 입력, Enter 디스패치,
메시지 버블 스크랩). UI selector는 `selectors.json`으로 외부화되어 있어, LINE이
업데이트되어도 코드 수정 없이 고칠 수 있습니다.

## 사전 준비

![별도 창으로 분리한 LINE Chrome 확장](docs/line-window.png)

*별도 Chrome 창으로 분리한 LINE 확장 — 이 도구가 조작하는 대상 상태입니다.*

1. **LINE Chrome 확장 설치**
   <https://chromewebstore.google.com/detail/line/ophjlpahpchlmihnnnihgmmeilfjmjjc>
2. **한 번 로그인** — 확장 아이콘을 클릭해 QR로 로그인. 이 단계는 절대 자동화하지
   마세요. LINE의 봇 감지가 인증 흐름을 주시합니다.
3. **확장을 별도 창으로 분리(detach).** AppleScript는 Chrome *탭*에는 JS를 주입할 수
   있지만 확장 팝업에는 못 합니다. 분리하면 탭 하나짜리 일반 Chrome 창이 됩니다
   (`…/index.html#/chats/…`).
4. **AppleScript JS 실행 활성화** — Chrome 메뉴
   `보기 → 개발자 정보 → Apple Events의 자바스크립트 허용`.
   `python3 cli.py enable-applescript`가 대신 해줍니다.
5. **손쉬운 사용 권한 부여** — `enable-applescript`에만 필요하며, System Events로
   Chrome 메뉴를 클릭합니다. `시스템 설정 → 개인정보 보호 → 손쉬운 사용`에
   터미널을 추가하세요.

## 설치

Python 3.9+ 와 `osascript`(macOS 기본 설치) 외에는 의존성이 없습니다.

### 1. 에이전트 스킬로 설치 (권장)

```sh
git clone https://github.com/ewhasan936/line-chrome-skill.git
```

이 저장소 디렉토리를 Claude Code, Codex 등 사용하는 에이전트가 스킬을 찾는 위치에
`line-chrome` 이름으로 두세요. 루트의 `SKILL.md`가 스킬 메타데이터이므로, 에이전트는
이 파일을 통해 `/line-chrome` 작업 방법과 내부 CLI 명령을 학습합니다.

설치 후 에이전트에게 이렇게 요청합니다:

```text
/line-chrome status 확인해줘
/line-chrome 김용진 대화 brief 해줘
/line-chrome 3분 뒤에 김용진한테 모하누라고 보내줘
```

또는 스킬 이름을 직접 쓰지 않아도, 에이전트가 이 스킬을 사용할 수 있다면 자연어로
요청하면 됩니다:

```text
LINE에서 김용진한테 뭐하냐고 보내줘
오늘 팀방에서 내가 답해야 할 메시지 찾아줘
```

### 2. 초기 연결 확인

에이전트가 내부적으로 실행할 CLI가 정상 동작하는지만 한 번 확인합니다:

```sh
cd line-chrome-skill
python3 cli.py status
python3 cli.py enable-applescript
```

### 3. 직접 CLI 사용 (선택)

에이전트 없이 셸에서 직접 쓰고 싶다면 `PATH`에 등록할 수 있습니다:

```sh
ln -s "$PWD/cli.py" /usr/local/bin/line-chrome
line-chrome status
```

## 사용법

### 에이전트에게 요청하기

일반 사용자는 보통 아래처럼 자연어로 요청합니다:

```text
/line-chrome 김용진 대화 요약해줘
/line-chrome 김용진한테 "모하누" 보내줘
/line-chrome 나만의 그룹에 미안해 스티커 보내줘
/line-chrome 내 LINE 받은편지함에서 답장 필요한 것 찾아줘
/line-chrome 김용진 방 tone profile 추천해줘
```

### 내부 CLI 명령

아래 명령들은 에이전트가 스킬 실행 중 내부적으로 호출하는 안정적인 인터페이스입니다.
고급 사용자는 직접 실행할 수도 있습니다.

```sh
python3 cli.py status                    # Chrome 연결됨? selector 로드됨?
python3 cli.py enable-applescript         # "Apple Events의 자바스크립트 허용" 켜기
python3 cli.py diagnose                   # 모든 selector를 라이브 DOM과 대조

python3 cli.py list-rooms --limit 50
python3 cli.py list-contacts --limit 50

python3 cli.py send --to "홍길동" --text "안녕하세요"
python3 cli.py history --room "Family" --limit 50
python3 cli.py search --room "Family" --query "회의"
python3 cli.py reply --room "Family" --to "6시에 보자" --text "알겠어"
python3 cli.py send-sticker --to "Family"   # 아래 "스티커" 참고
python3 cli.py send-sticker --to "Family" --meaning thanks
python3 cli.py sticker-tags set thanks --package 0 --sticker 3 --label "고마워"
python3 cli.py brief --room "Family" --room "Team"
python3 cli.py needs-reply --room "Team"
python3 cli.py tone-profiles set polite --prefix "안녕하세요. " --suffix " 감사합니다."
python3 cli.py tone-profiles assign "Team" --profile polite
python3 cli.py follow-ups add --room "Team" --text "답변 확인" --in 2h
python3 cli.py schedule add --to "Team" --text "스탠드업 시간입니다" --at "2030-01-02 09:00"
python3 cli.py schedule run --dry-run
python3 cli.py allowed-rooms add "나만의 그룹"
python3 cli.py allowed-rooms enable
python3 cli.py leave-group --room "Old Group" --confirm   # 되돌릴 수 없음 — 아래 참고
python3 cli.py watch --interval 5         # 새 메시지 폴링 (Ctrl-C로 중지)

python3 cli.py selectors show
python3 cli.py selectors set message_input "textarea-ex.text"

python3 cli.py cache-info                 # 확장의 LevelDB 저장소 위치 확인
python3 cli.py cache-dump --out ~/line-cache-copy
```

모든 명령은 JSON을 stdout으로 출력합니다.

## 테스트

```sh
python3 -m unittest tests/test_send_sticker_contract.py
LINE_TEST_ONLY=sticker LINE_TEST_ROOM="나만의 그룹" python3 tests/test_reply_sticker.py
```

첫 번째 명령은 Chrome이나 LINE을 건드리지 않고 `send-sticker`의 JSON/검증 계약을
확인합니다. 두 번째 명령은 설정한 테스트 방에서만 스티커 라이브 매트릭스를 실행하며,
hot/cold 경로의 1초 미만 지연 시간까지 확인합니다.

### `enable-applescript`

AppleScript JS 실행이 켜져 있는지 확인합니다. 꺼져 있으면 Chrome을 앞으로 가져와
System Events로 `보기 → 개발자 정보 → Apple Events의 자바스크립트 허용`을 클릭하고,
다시 확인합니다. 설정이 **꺼져 있을 때만** 클릭하므로, 실수로 다시 끄는 일은
없습니다.

참고: Chrome은 이 설정을 AppleScript로 **끄는** 것은 차단합니다 — 켜는 방향만
자동화 가능하며, 이 명령에는 그것으로 충분합니다.

## 매일 쓰는 자동화

`brief`는 최근 메시지를 훑어 방별 메시지 수, 최근 미리보기, 질문/요청 수, 답장이
필요해 보이는 항목, 그리고 `summary.text` 대화 요약을 JSON으로 반환합니다. 방을
지정하지 않으면 현재 열린 방을 한 번만 읽는 빠른 경로를 사용합니다. 여러 방을 지정할
수 있지만, 기본 `--max-runtime-ms 900` 예산을 넘기면 남은 방은 `deadline_exceeded`로
표시됩니다.

```sh
python3 cli.py brief --room "Family" --room "Team" --limit 50
python3 cli.py daily-brief --rooms "Family,Team" --preview 3 --max-runtime-ms 1500
```

`needs-reply`는 마지막으로 내가 보낸 메시지 이후의 수신 메시지 중 질문/요청 표현이 있는
항목을 모읍니다. 방을 지정하지 않으면 현재 열린 방을 빠르게 읽고, `--max-runtime-ms`로
여러 방 스캔의 시간 예산을 조정할 수 있습니다.

```sh
python3 cli.py needs-reply --room "Team"
python3 cli.py inbox --rooms "Family,Team" --include-before-last-sent
```

`tone-profiles`는 방별 말투를 수동 프로필로 저장합니다. 현재 구현은 LLM으로 문체를
재작성하지 않고, 사용자가 정한 prefix/suffix를 적용합니다.

```sh
python3 cli.py tone-profiles set polite --prefix "안녕하세요. " --suffix " 감사합니다."
python3 cli.py tone-profiles assign "Team" --profile polite
python3 cli.py send --to "Team" --text "확인했습니다"   # 프로필 자동 적용
python3 cli.py send --to "Team" --text "확인했습니다" --no-profile
```

`follow-ups`는 로컬 리마인더입니다. `send`/`reply`에도 `--follow-up-in` 또는
`--follow-up-at`을 붙이면 전송 성공 후 자동으로 항목을 남깁니다.

```sh
python3 cli.py follow-ups add --room "Team" --text "답변 확인" --in 2h
python3 cli.py follow-ups due
python3 cli.py send --to "Team" --text "확인 부탁드립니다" --follow-up-in 1d
```

`schedule`은 예약 발송 큐입니다. 백그라운드 데몬을 띄우지는 않으므로, 실제 발송은
`schedule run`을 `cron`이나 `launchd`에서 주기적으로 실행해 처리합니다.

```sh
python3 cli.py schedule add --to "Team" --text "스탠드업 시간입니다" --at "2030-01-02 09:00"
python3 cli.py schedule add --to "Team" --text "오늘 공유할 이슈를 올려주세요" --in 10m
python3 cli.py schedule run
```

`allowed-rooms`는 발신 안전장치입니다. 활성화하면 `send`, `reply`, `send-sticker`,
`leave-group`, `schedule add/run`이 허용 목록 밖의 방에는 Chrome을 건드리기 전에
실패합니다.

```sh
python3 cli.py allowed-rooms add "나만의 그룹"
python3 cli.py allowed-rooms enable
python3 cli.py allowed-rooms show
```

### `reply`

`reply --room R --to "<부분문자열>" --text "<본문>"` — 특정 이전 메시지에 인용
답장을 보냅니다. `--to`는 답장 대상 메시지를 식별하는 부분 문자열이며, 여러 개가
일치하면 가장 최근 것을 사용합니다. 이미 방에 있으면 약 0.4초, 방 이동이 필요하면
약 0.8초에 완료됩니다.

### `send-sticker`

`send-sticker --to R [--package N] [--sticker N]` — 패키지/스티커 인덱스로 지정해
스티커를 보냅니다 (기본 `0 0` = 첫 패키지의 첫 스티커).
`send-sticker --to R --meaning TAG`는 `~/.config/line-chrome/stickers.json`에 저장된
태그 스티커를 보냅니다.

태그 생성/수정 예:

```sh
python3 cli.py sticker-tags set thanks --package 0 --sticker 3 --label "고마워"
python3 cli.py sticker-tags set sorry --package 0 --sticker 7 --label "미안"
python3 cli.py sticker-tags show
python3 cli.py sticker-tags remove thanks
```

태그는 사용자가 정하는 문자열이라 `고마워`, `감사`, `미안` 같은 한국어 태그도 그대로
쓸 수 있습니다. `--meaning`이 매핑되어 있지 않으면 Chrome이나 LINE을 건드리기 전에
`{"ok": false, "reason": "meaning_not_mapped"}`를 반환합니다.
LINE 캐릭터 스티커도 본인의 피커 순서에서 패키지/스티커 인덱스를 먼저 확인한 뒤
`미안해`, `고마워`, `축하` 같은 의미 태그로 등록해 사용할 수 있습니다.

LINE 스티커 피커를 여는 것은 *신뢰된(trusted)* 사용자 활성화 제스처를 요구하므로,
`send-sticker`는 macOS CoreGraphics 세션 이벤트로 Chrome 안을 OS 레벨 클릭합니다.
CLI를 실행하는 터미널 또는 앱에 손쉬운 사용 권한을 부여해야 합니다. 신뢰된 입력을 사용할 수 없으면
명확히 `{"ok": false, "reason": "trusted_input_unavailable"}`를 반환합니다.

성공 여부는 새 스티커 메시지 버블이 생겼는지로 검증합니다. 음수 인덱스는 LINE을
건드리기 전에 거절하고, 범위를 벗어난 패키지/스티커 인덱스는 `ok: false` JSON으로
깨끗하게 실패합니다.

### `leave-group`

그룹에서 영구히 나갑니다. **되돌릴 수 없습니다** — 한 번 나가면 스스로 다시
참여할 수 없고, 그룹의 현재 멤버가 다시 초대해 줘야 합니다.

그래서 `leave-group`은 2단계 확인을 거칩니다:

1. 먼저 `--confirm` **없이** 실행합니다. 파괴적 동작은 일어나지 않고,
   `reason: "confirmation_required"`와 `warning`을 반환합니다:
   ```sh
   python3 cli.py leave-group --room "Old Group"
   ```
2. 경고를 확인하고 정말 나갈지 결정한 뒤, `--confirm`을 **붙여** 다시 실행합니다:
   ```sh
   python3 cli.py leave-group --room "Old Group" --confirm
   ```

이 명령은 동작 전에 열린 채팅방 헤더가 `--room`과 일치하는지 검증하고, 메뉴나 확인
모달이 예상대로 나타나지 않으면 파괴적 단계 **이전에** 중단합니다. AI 어시스턴트가
대신 실행하는 경우, `--confirm`을 붙이기 전에 경고를 사용자에게 보여주고 명시적인
동의("그룹을 나가면 다시 참여할 수 없습니다. 진행하시겠습니까?")를 받아야 합니다.

## 깨진 selector 고치기

LINE이 확장을 업데이트하면 selector가 매칭되지 않을 수 있습니다. 코드 수정은
필요 없습니다:

1. `python3 cli.py diagnose` — 어떤 selector 키가 실패하는지 알려줍니다.
2. LINE 확장 창 안에서 DevTools를 열고 요소를 inspect.
3. 안정적인 selector 선택 (`data-*` / `aria-*` / `role` > class > tag 순으로 선호).
4. 우선순위에 따라 override:
   - **일회성:** 아무 명령에 `--selector message_input='…'` (반복 가능).
   - **영구적:** `~/.config/line-chrome/selectors.json`
     ```json
     { "selectors": { "message_input": "textarea-ex.text" } }
     ```
   - **repo 기본값:** 이 디렉토리의 `selectors.json` 편집.
5. `diagnose`를 다시 실행해 확인.

selector 우선순위: `--selector` 플래그 > `~/.config/line-chrome/selectors.json` > repo
`selectors.json`. 12개 키 중 9개는 라이브 DOM에서 검증되었고, `search_input`,
`send_button`, `message_author`는 generic fallback으로 제공됩니다. (`message_author`는
안정적인 요소가 없습니다 — 작성자 이름은 `data-message-content-prefix` 속성에서
읽으므로, `diagnose`가 미매칭으로 보고하는 것은 정상이며 무해합니다.)

## 메시지 기록 & 전문 검색

`history`와 `search`는 렌더된 DOM을 스크랩하므로, 채팅 화면에 현재 로드된 메시지만
봅니다. 깊은 기록은 확장의 IndexedDB(LevelDB)에 모두 저장되어 있습니다:

- `cache-info` — 저장소 위치, 크기, 마지막 수정 시각 표시.
- `cache-dump --out <dir>` — Chrome이 락을 쥔 상태에서 best-effort 스냅샷 복사
  (`cp -R`). 깨끗하게 읽으려면 먼저 Chrome을 종료하세요.

LevelDB + V8 직렬화 항목 디코딩은 범위 밖입니다. 덤프한 사본에 외부 도구
(예: Node의 `level` + `v8` deserialize)를 사용하세요.

## 주의사항

- **macOS 전용.** Chrome으로의 AppleScript 브리지를 사용합니다.
- **첫 로그인은 수동.** QR 인증 흐름을 스크립트로 자동화하지 마세요.
- **확장 창을 분리하세요.** 팝업은 AppleScript로 접근할 수 없습니다.
- **selector는 변할 수 있습니다.** LINE 업데이트가 클래스 해시를 바꿀 수 있으니
  위의 수정 절차를 참고하세요.
- 이것은 비공식 도구이며 LINE과 제휴되거나 LINE의 보증을 받지 않습니다.

## 라이선스

[MIT](LICENSE)
