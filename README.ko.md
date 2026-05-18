# line-chrome-cli

[English](README.md) | **한국어** | [日本語](README.ja.md)

**공식 LINE Chrome 확장**을 커맨드라인에서 조작하거나, AI 어시스턴트가 대신
조작하게 합니다. 메시지 보내기, 대화 따라잡기, 요약, 검색, 답장 감시 — 모두
AppleScript의 `execute javascript` 브리지로 JavaScript를 주입해 처리합니다.

LINE 데스크톱 앱을 건드리거나, 토큰을 빼내거나, LINE의 비공개 API를
리버스엔지니어링하지 **않습니다**. 모든 발신 메시지는 사용자 본인이 로그인한 LINE
확장이 생성하며, UI에서 직접 타이핑한 것과 동일합니다.

> macOS 전용. AppleScript ↔ Chrome 브리지에 의존합니다.

<!-- 선택: 데모 스크린샷이나 GIF를 여기에 넣으세요. 예: ![demo](docs/demo.gif) -->

## 무엇을 할 수 있나요

[Claude Code](https://claude.com/claude-code) 스킬로 설치하거나(또는 다른 에이전트·셸
스크립트에 연결하고), 자연어로 요청하면 됩니다:

| 이렇게 말하면 | 이런 일이 일어납니다 |
| --- | --- |
| "김용진한테 5분 늦는다고 보내줘" | 채팅방을 찾아 입력하고 전송 — 전달까지 검증 |
| "팀 채팅방에서 내가 놓친 거 알려줘" | 최근 메시지를 읽고 정리해 보고 |
| "가족 그룹 오늘 대화 요약해줘" | 대화 기록을 가져와 요약 |
| "어제 용진이가 보낸 주소 찾아줘" | 채팅방 메시지에서 키워드 검색 |
| "프로젝트방에 누가 답하면 알려줘" | 새 수신 메시지를 감시 |

### 의미 있는 활용 시나리오

- **아침 따라잡기** — 밤사이 쌓인 여러 채팅방 메시지를 한 번에 요약.
- **답장 초안 작성** — 어시스턴트가 스레드를 읽고 맥락에 맞는 답장을 초안으로
  작성하면, 사용자가 승인 후 전송.
- **받은편지함 분류** — "내가 아직 답 안 한 직접 질문 있어?"
- **예약 리마인더** — `cron`과 결합: 평일 오전 9시마다 채팅방에 스탠드업 알림 전송.
- **아카이빙** — 채팅방 기록을 파일로 덤프해 보관.

이 모든 것은 일반 CLI 서브커맨드(`send`, `history`, `search`, `watch` 등)로
매핑되므로, 에이전트 없이 셸 스크립트 안에서도 동일하게 동작합니다.

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

```sh
git clone https://github.com/ewhasan936/line-chrome-cli.git
cd line-chrome-cli
python3 cli.py status
```

선택적으로 `PATH`에 등록:

```sh
ln -s "$PWD/cli.py" /usr/local/bin/line-chrome
line-chrome status
```

Claude Code 스킬로 쓰려면 이 디렉토리를 Claude Code가 스킬을 찾는 위치에 두세요 —
포함된 `SKILL.md`가 자동 발견되게 해줍니다.

## 사용법

```sh
python3 cli.py status                    # Chrome 연결됨? selector 로드됨?
python3 cli.py enable-applescript         # "Apple Events의 자바스크립트 허용" 켜기
python3 cli.py diagnose                   # 모든 selector를 라이브 DOM과 대조

python3 cli.py list-rooms --limit 50
python3 cli.py list-contacts --limit 50

python3 cli.py send --to "홍길동" --text "안녕하세요"
python3 cli.py history --room "Family" --limit 50
python3 cli.py search --room "Family" --query "회의"
python3 cli.py leave-group --room "Old Group" --confirm   # 되돌릴 수 없음 — 아래 참고
python3 cli.py watch --interval 5         # 새 메시지 폴링 (Ctrl-C로 중지)

python3 cli.py selectors show
python3 cli.py selectors set message_input "textarea-ex.text"

python3 cli.py cache-info                 # 확장의 LevelDB 저장소 위치 확인
python3 cli.py cache-dump --out ~/line-cache-copy
```

모든 명령은 JSON을 stdout으로 출력합니다.

### `enable-applescript`

AppleScript JS 실행이 켜져 있는지 확인합니다. 꺼져 있으면 Chrome을 앞으로 가져와
System Events로 `보기 → 개발자 정보 → Apple Events의 자바스크립트 허용`을 클릭하고,
다시 확인합니다. 설정이 **꺼져 있을 때만** 클릭하므로, 실수로 다시 끄는 일은
없습니다.

참고: Chrome은 이 설정을 AppleScript로 **끄는** 것은 차단합니다 — 켜는 방향만
자동화 가능하며, 이 명령에는 그것으로 충분합니다.

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
