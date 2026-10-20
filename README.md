<p align="center">
  <img src="rectifier/assets/logo/logo-128.png" width="128" height="128" alt="묑류계">
</p>

<h1 align="center">묑류계</h1>

<p align="center">디시인사이드 역류기 대응 정류기 — 마이너 갤러리 관리자용 글 정리·도배 방어·끌올 도구</p>

<p align="center">
  <a href="https://github.com/igayowai/Jinhsi-rectifier/releases/latest/download/Jinhsi-rectifier.exe">
    <img src=".github/download-button.svg" width="440" alt="Windows용 다운로드">
  </a>
</p>

<p align="center">Windows 10 / 11 · 무료 · <a href="https://github.com/igayowai/Jinhsi-rectifier/releases">이전 버전·변경 내역</a></p>

---

## 시작하기

1. 위 버튼으로 `Jinhsi-rectifier.exe`를 받아 실행함. 설치는 필요 없음.
   - 처음 실행할 때 "Windows의 PC 보호" 창이 뜨면 `추가 정보` → `실행`을 누름(코드 서명이 없는 프로그램이라 뜨는 경고).
2. 본인 디시 아이디·비밀번호로 로그인함. 보안문자가 나오면 직접 입력함.
3. 위쪽 `갤러리` 칸에 관리하는 마이너 갤러리 ID(주소의 `id=` 뒤 값)를 넣음. 한 번 조회한 갤러리는 다음부터 목록에 남음.

> 본인이 관리 권한을 가진 갤러리에서 본인 계정으로만 사용할 것.

## 할 수 있는 일

**목록 정리**
- `최신 1페이지`·`1~3페이지`·`1~5페이지` 버튼으로 바로 조회. 비회원은 주황, 개념글은 파랑, 판별 불가는 회색으로 표시
- `비회원 일반글 선택` 한 번으로 도배 글만 골라 `선택 삭제`(또는 `Delete` 키)
- 개념글은 골라서 `선택 개념글 해제`
- 실행 전 표 아래 확인 카드에서 대상을 한 번 더 보여 주고, 끝나면 목록을 자동으로 다시 불러옴

**자동 방어**
- 위쪽 `방어 켜기` 한 번으로 어느 화면에서든 시작, `방어 끄기`로 멈춤
- 켠 시점 이후 올라오는 비회원 글(선택 시 신규 회원 글 포함)을 계속 지움. 켜기 전부터 있던 글과 공지는 건드리지 않음
- 경과 시간·처리 속도·누적 처리 수를 실시간으로 표시

**끌올**
- 글 주소를 넣고 정한 간격으로 반복 끌올. 여러 글(최대 20개)을 돌아가며 끌올할 수도 있음
- 목록에서 고른 글을 `끌올로 보내기`로 바로 넘길 수 있음

## 알아둘 것

- 계정은 이 PC에만 저장됨. 비밀번호는 Windows 자격 증명 관리자에 보관되며 다른 곳으로 보내지 않음.
- 이미 보낸 삭제 요청은 되돌릴 수 없음. 삭제된 글을 복구하는 기능은 없음.
- 한 번에 최대 5페이지·250개까지 처리함. 디시 쪽에서 요청 제한이나 삭제 한도에 걸리면 멈추고 이유를 알려 줌.
- 창 크기와 글자는 모니터 배율에 맞춰 자동으로 조절됨.

## 소스로 실행 (개발자용)

Python 3.14에서 확인함.

```powershell
python -m pip install -r requirements.txt
python -B -m rectifier ui
```

- 검사: `python -B -m unittest discover -s tests -t .` (실제 디시 서버에 요청하지 않는 합성 검사)
- EXE 직접 만들기: `pip install -r packaging/requirements-build.txt` 후 `python packaging/build.py --format portable`

## 라이선스

코드는 [MIT 라이선스](LICENSE)를 따름. `rectifier/assets`의 앱 아이콘 그림은 라이선스 대상이 아니며 원저작자에게 권리가 있음.
