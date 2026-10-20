import argparse
from dataclasses import asdict
from getpass import getpass, GetPassWarning
import json
from pathlib import Path
import sys
import warnings

from . import __version__, APP_NAME

from .demo import DEMO_HTML
from .models import validate_gallery
from .parser import parse_board, parse_board_entries
from .policy import build_plan
from .page_preview import classify_list_entries
from .public_board import PublicReadError, read_public_board
from .credentials import CredentialError, read_password, save_password_new, forget_password
from .captcha_ui import prompt_captcha


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo unrecognized arguments: someone might accidentally paste a password.
        self.exit(2, "입력 인자 오류. --help 참고. 비밀번호는 명령 인자로 넣지 않아야 함.\n")


def _hidden_password():
    if not sys.stdin.isatty():
        raise CredentialError("비밀번호를 가려 입력할 수 있는 대화형 터미널에서 실행 필요함")
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        return getpass("디시 비밀번호 (화면·파일에 표시하지 않음): ")


def login_check(account, *, manual=False, gallery=None):
    from .auth import login
    result = None
    password = None
    try:
        if gallery is not None:
            try:
                validate_gallery(gallery)
            except ValueError:
                print("중지: 갤러리 ID 형식 오류", file=sys.stderr)
                return 2
        password = None if manual else read_password(account)
        source = "windows_credential_manager" if password is not None else "manual"
        if password is None:
            password = _hidden_password()
        result = login(account, password, captcha_prompt=prompt_captcha)
        output = {"mode": "login_check", "status": result.status, "message": result.message,
                  "password_source": source,
                  "identity_verified": result.identity_verified,
                  "verified_user_id": result.verified_user_id,
                  "diagnostics": [asdict(step) for step in result.diagnostics],
                  "live_delete_available": False, "session_discarded_after_check": True}
        if gallery is not None:
            from .management import check_management
            output["management"] = check_management(result, gallery)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if gallery is not None:
            return 0 if output["management"]["status"] == "control_observed" else 2
        return 0 if result.accepted else 2
    except (GetPassWarning, EOFError):
        print("중지: 비밀번호 가림 입력을 사용할 수 없음", file=sys.stderr)
        return 2
    except CredentialError as error:
        print(f"중지: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자가 로그인 검사를 중지함", file=sys.stderr)
        return 130
    except Exception:
        print("중지: 로그인 검사 오류. 인증 응답의 원문은 출력하지 않음", file=sys.stderr)
        return 2
    finally:
        if result is not None and result.session is not None:
            result.session.close()
        password = None


def credential_command(account, *, forget=False):
    password = None
    try:
        if forget:
            removed = forget_password(account)
            print("정류기 저장 비밀번호 제거됨" if removed else "정류기 저장 비밀번호 없음")
        else:
            password = _hidden_password()
            save_password_new(account, password)
            print("정류기 전용 Windows 자격 증명 저장 완료")
        return 0
    except CredentialError as error:
        print(f"중지: {error}", file=sys.stderr)
        return 2
    except (GetPassWarning, EOFError):
        print("중지: 비밀번호 가림 입력을 사용할 수 없음", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자가 작업을 중지함", file=sys.stderr)
        return 130
    except Exception:
        print("중지: 자격 증명 작업 오류. 원문은 출력하지 않음", file=sys.stderr)
        return 2
    finally:
        password = None


def main(argv=None):
    parser = SafeArgumentParser(description=f"{APP_NAME}: 디시인사이드 정류기")
    parser.add_argument('--version', action='version', version=f'{APP_NAME} {__version__}')
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ui", help="수동 조회·선택 삭제·개념글 해제·방어 화면")
    preview = commands.add_parser("preview", help="지정 글 또는 목록 페이지의 후보 확인; 실제 삭제 없음")
    preview.add_argument("--gallery", required=True)
    preview.add_argument("--account", required=True, help="예상 계정 식별 ID; 입력 자체는 인증이 아님")
    selection = preview.add_mutually_exclusive_group(required=True)
    selection.add_argument("--ids", type=int, nargs="+", help="고정 글 번호 최대 3개")
    selection.add_argument("--all-on-page", action="store_true", help="지정 목록 페이지의 전체 일반글 미리보기")
    source = preview.add_mutually_exclusive_group(required=True)
    source.add_argument("--html", type=Path, help="로컬 목록 HTML")
    source.add_argument("--page", type=int, help="비로그인 공개 목록 한 페이지를 한 번 조회")
    source.add_argument("--pages", type=int, nargs="+", help="--all-on-page 전용: 지정한 1~5페이지 순차 조회")
    commands.add_parser("demo", help="합성 목록 HTML 분류 미리보기; 네트워크·삭제 없음")
    auth = commands.add_parser("login-check", help="직접 로그인 요청 검사; 세션 저장·실삭제 없음")
    auth.add_argument("--account", required=True, help="닉네임이 아닌 로그인 ID")
    auth.add_argument("--manual", action="store_true", help="저장소를 사용하지 않고 직접 가림 입력")
    auth.add_argument("--gallery", help="로그인 후 지정 갤러리의 관리 버튼을 한 번 조회; 삭제 없음")
    save = commands.add_parser("password-save", help="정류기 전용 Windows 자격 증명 최초 등록")
    save.add_argument("--account", required=True)
    forget = commands.add_parser("password-forget", help="지정 계정의 정류기 저장 비밀번호만 제거")
    forget.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    if args.command == "ui":
        from .ui import run_ui
        return run_ui()
    if args.command == "login-check":
        return login_check(args.account, manual=args.manual, gallery=args.gallery)
    if args.command in {"password-save", "password-forget"}:
        return credential_command(args.account, forget=args.command == "password-forget")
    try:
        if args.command == "preview":
            if args.pages is not None:
                if not args.all_on_page:
                    raise ValueError('--pages는 --all-on-page와 함께 사용해야 함')
                from .page_preview import preview_pages
                output = preview_pages(args.gallery, args.account, args.pages)
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 0 if output['requested_pages_completed'] else 2
            # Page preview changes selection only; execution bounds stay unchanged.
            if args.all_on_page:
                validate_gallery(args.gallery)
                if not args.account.strip():
                    raise ValueError("예상 로그인 ID가 필요함")
            else:
                build_plan(args.gallery, args.account, args.ids, [])
            html = args.html.read_text(encoding="utf-8") if args.html else read_public_board(args.gallery, args.page)
            if args.all_on_page:
                classified = classify_list_entries(args.gallery, parse_board_entries(html, args.gallery))
                output = {"mode": "preview_only", "scope": "all_on_page",
                          "gallery_id": args.gallery, "account_id": args.account.strip(),
                          "source": "local_html" if args.html else "public_page",
                          "page": args.page, "authenticated": False,
                          **classified,
                          "live_delete_available": False, "delete_requests": 0}
            else:
                posts = parse_board(html, args.gallery)
                plan = build_plan(args.gallery, args.account, args.ids, posts)
                output = {"mode": "preview_only", "authenticated": False, "plan": asdict(plan),
                          "target_count": len(plan.targets), "live_delete_available": False}
        else:
            posts = parse_board(DEMO_HTML, "mnt")
            plan = build_plan("mnt", "synthetic_account", [1, 2, 3], posts)
            output = {"mode": "offline_demo", "plan": asdict(plan),
                      "target_count": len(plan.targets), "live_requests": 0}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, PublicReadError) as error:
        print(f"중지: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
