"""One anonymous list-page GET. No browser profile, login or cookie store."""
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import validate_gallery, validate_post_id


MAX_HTML_BYTES = 2_000_000


class PublicReadError(RuntimeError):
    pass


class PublicServerError(PublicReadError):
    """DCInside answered 5xx: its own outage, not the user's network."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_public_board(gallery_id: str, page: int) -> str:
    validate_gallery(gallery_id)
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("페이지 번호 오류")
    url = "https://gall.dcinside.com/mgallery/board/lists/?" + urlencode({"id": gallery_id, "page": page})
    return _read_public_html(url)


def read_public_post(gallery_id: str, post_id: int) -> str:
    validate_gallery(gallery_id)
    validate_post_id(post_id)
    url = "https://gall.dcinside.com/mgallery/board/view/?" + urlencode({'id': gallery_id, 'no': post_id})
    return _read_public_html(url)


def _read_public_html(url):
    request = Request(url, headers={"User-Agent": "Rectifier/0.1 (single-page moderation preview)",
                                    "Accept": "text/html"})
    try:
        # A new opener intentionally has no cookie processor. No retries or login redirects.
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200 or response.headers.get_content_type() != "text/html":
                raise PublicReadError("정상 HTML 목록 응답이 아님")
            data = response.read(MAX_HTML_BYTES + 1)
            if len(data) > MAX_HTML_BYTES:
                raise PublicReadError("목록 크기 제한 초과")
            return data.decode(response.headers.get_content_charset() or "utf-8", errors="strict")
    except HTTPError as error:
        code = error.code
        error.close()
        if code >= 500:
            raise PublicServerError(f"HTTP {code}: 디시 서버 장애, 자동 재시도 없음") from None
        raise PublicReadError(f"HTTP {code}: 조회 중지, 자동 재시도 없음") from None
    except (URLError, TimeoutError, OSError, UnicodeError, LookupError):
        raise PublicReadError("목록 조회 실패; 로그인·권한·네트워크 또는 인코딩 확인 필요") from None
