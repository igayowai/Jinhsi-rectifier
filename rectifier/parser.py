"""Conservative parsing of PC minor-gallery lists, using synthetic-tested shapes.

No requests, browser state, author nicknames or IPs leave this parser. Ambiguous
author fields yield UNKNOWN; ambiguous page/post identity raises BoardParseError.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urlsplit

from .models import AuthorKind, Post, validate_gallery


class BoardParseError(ValueError):
    """The input cannot safely be treated as the requested gallery's list."""


_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"})
_INERT = frozenset({"script", "style", "template", "noscript"})
_SPECIAL_NUMBERS = frozenset({"공지", "설문", "광고", "투표", "AD"})
_SPECIAL_TYPES = frozenset({"icon_notice", "icon_survey", "icon_ad", "icon_ads", "icon_vote"})
_NUMBER = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)


class PostKind(StrEnum):
    ORDINARY = 'ordinary'
    RECOMMENDED = 'recommended'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ListEntry:
    # List-only metadata: never add this to archived Post/fingerprint fields.
    post: Post
    post_kind: PostKind
    new_member_badge: bool = False

    def __post_init__(self):
        if type(self.new_member_badge) is not bool:
            raise ValueError('신규 회원 딱지 상태 오류')


def _new_member_badge(row, post):
    if post.author_kind is not AuthorKind.MEMBER or not post.uid:
        return False
    writers = [n for n in row.children
               if isinstance(n, _Node) and n.tag == 'td' and 'gall_writer' in n.classes]
    if len(writers) != 1:
        return False
    badges = [n for n in writers[0].elements() if 'writer_nikcon' in n.classes]
    if len(badges) != 1 or badges[0].bad_attrs:
        return False
    nodes = tuple(badges[0].elements())
    if any(n.bad_attrs or n.tag in _INERT for n in nodes):
        return False
    images = [n for n in nodes if n.tag == 'img']
    if len(images) != 1:
        return False
    try:
        url = urlsplit(images[0].attrs.get('src') or '')
        return (url.scheme in ('', 'https') and url.netloc == 'nstatic.dcinside.com'
                and not url.fragment and url.path in {
                    '/dc/w/images/newnik.gif', '/dc/w/images/fix_newnik.gif'})
    except ValueError:
        return False


def _post_kind(row):
    value = row.attrs.get('data-type')
    if value in {'icon_recomimg', 'icon_recomtxt'}:
        return PostKind.RECOMMENDED
    # Only positively observed ordinary types; absence is not ordinary.
    if value in {'icon_txt', 'icon_pic'}:
        return PostKind.ORDINARY
    return PostKind.UNKNOWN


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    bad_attrs: bool = False
    children: list["_Node | str"] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def elements(self, tag: str | None = None):
        for child in self.children:
            if isinstance(child, _Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.elements(tag)

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text()
                       for child in self.children if not isinstance(child, _Node)
                       or child.tag not in _INERT)


class _ListHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[_Node] = []
        self.stack: list[_Node] = []
        self.inert: list[str] = []
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        names = [key for key, _ in attrs]
        node = _Node(tag, dict(attrs), len(names) != len(set(names)))
        if not self.stack:
            if tag in _INERT:
                self.inert.append(tag)
                return
            if self.inert:
                return
            is_list = any(key == "class" and "gall_list" in (value or "").split()
                          for key, value in attrs)
            if tag == "table" and is_list:
                self.tables.append(node)
                self.stack.append(node)
            return

        parent = self.stack[-1]
        if tag == "table" or len(self.stack) >= 64:
            raise BoardParseError("목록 표의 중첩 구조가 잘못됨")
        if tag == "tr" and parent.tag not in {"table", "tbody", "thead", "tfoot"}:
            self.invalid = True
        if tag in {"tbody", "thead", "tfoot"} and parent.tag != "table":
            self.invalid = True
        if tag in {"td", "th"} and parent.tag != "tr":
            self.invalid = True
        parent.children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if not self.stack:
            if self.inert and tag == self.inert[-1]:
                self.inert.pop()
            return
        if tag in _VOID:
            return
        if self.stack[-1].tag == tag:
            self.stack.pop()
            return
        self.invalid = True
        # Recover only enough to find subsequent tables; never accept this table.
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)


def _validate_link(anchor: _Node, gallery_id: str, post_id: int, *, reply=False):
    href = anchor.attrs.get("href")
    if anchor.bad_attrs or not href or any(ord(char) <= 32 or ord(char) == 127 for char in href):
        raise BoardParseError("글 링크 필드가 잘못됨")
    if href.startswith("/") and not href.startswith("//"):
        href = "https://gall.dcinside.com" + href
    try:
        url = urlsplit(href)
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=50)
    except ValueError as exc:
        raise BoardParseError("글 링크를 해석할 수 없음") from exc
    if (url.scheme != "https" or url.netloc != "gall.dcinside.com"
            or url.path not in {"/mgallery/board/view/", "/mgallery/board/view"}):
        raise BoardParseError("마이너 갤러리 글 링크가 아님")
    keys = [key for key, _ in pairs]
    values = dict(pairs)
    if len(keys) != len(set(keys)):
        raise BoardParseError("글 링크의 중복 query 필드")
    if values.get("id") != gallery_id or values.get("no") != str(post_id):
        raise BoardParseError("대상 갤러리 또는 글 번호가 링크와 일치하지 않음")
    allowed_fragments = {"", "comment_view", "comment_wrap"} if reply else {""}
    if url.fragment not in allowed_fragments:
        raise BoardParseError("예상하지 못한 글 링크 fragment")


def _valid_ip_display(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) in {2, 4} and all(
        re.fullmatch(r"0|[1-9][0-9]{0,2}", part, re.ASCII) and int(part) <= 255
        for part in parts
    )


def _author(writers: list[_Node]) -> tuple[AuthorKind, str]:
    if len(writers) != 1 or writers[0].bad_attrs:
        return AuthorKind.UNKNOWN, ""
    writer = writers[0]
    if any(node.tag in _INERT or "gall_writer" in node.classes for node in writer.elements()):
        return AuthorKind.UNKNOWN, ""
    # Absence and an explicitly empty data-uid have different meanings.
    if "data-uid" not in writer.attrs or writer.attrs["data-uid"] is None:
        return AuthorKind.UNKNOWN, ""
    uid = writer.attrs["data-uid"]
    ip = writer.attrs.get("data-ip")
    if uid:
        if not _UID.fullmatch(uid) or ip not in {None, ""}:
            return AuthorKind.UNKNOWN, ""
        return AuthorKind.MEMBER, uid
    if _valid_ip_display(ip):
        return AuthorKind.GUEST, ""
    return AuthorKind.UNKNOWN, ""


def _parse_row(row: _Node, gallery_id: str) -> Post | None:
    cells = [child for child in row.children if isinstance(child, _Node) and child.tag == "td"]
    if any(len(cell.classes & {"gall_num", "gall_tit", "gall_writer"}) > 1 for cell in cells):
        raise BoardParseError("한 열에 여러 게시글 필드가 겹침")
    numbers = [cell for cell in cells if "gall_num" in cell.classes]
    if not numbers:
        # A proper column header or an empty tbody does not contain posts.
        if any(isinstance(child, _Node) and child.tag == "th" for child in row.children):
            return None
        raise BoardParseError("목록 행에서 글 번호 열을 확인할 수 없음")
    if len(numbers) != 1 or numbers[0].bad_attrs or row.bad_attrs:
        raise BoardParseError("글 번호 또는 행 속성이 중복됨")
    number = numbers[0].text().strip()
    if number in _SPECIAL_NUMBERS or row.attrs.get("data-type") in _SPECIAL_TYPES:
        return None
    # Public PC lists also put a survey, an advertisement and DCInside-wide
    # operator notices above normal rows: gall_num='-',
    # gall_subject='설문'/'AD'/'공지', class='ub-content', no post attrs.
    # Do not treat arbitrary nonnumeric rows, normal posts or unknown subjects
    # as this shape, and never inspect/follow their external action links.
    if number == "-" and row.classes == {"ub-content"} and set(row.attrs) == {"class"}:
        subjects = [cell for cell in cells if "gall_subject" in cell.classes]
        if (len(subjects) == 1 and not subjects[0].bad_attrs
                and subjects[0].text().strip() in {"설문", "AD", "공지"}):
            return None
    if not _NUMBER.fullmatch(number):
        raise BoardParseError("일반 글의 번호 형식이 잘못됨")
    post_id = int(number)
    if "data-no" in row.attrs and row.attrs["data-no"] != number:
        raise BoardParseError("행 속성과 표시 글 번호가 일치하지 않음")
    titles = [cell for cell in cells if "gall_tit" in cell.classes]
    writers = [cell for cell in cells if "gall_writer" in cell.classes]
    if len(titles) != 1 or titles[0].bad_attrs:
        raise BoardParseError("제목 열이 없거나 중복됨")
    anchors = list(titles[0].elements("a"))
    primary = [anchor for anchor in anchors if "reply_numbox" not in anchor.classes]
    replies = [anchor for anchor in anchors if "reply_numbox" in anchor.classes]
    if len(primary) != 1 or len(replies) > 1:
        raise BoardParseError("제목 링크가 없거나 중복됨")
    _validate_link(primary[0], gallery_id, post_id)
    for reply in replies:
        _validate_link(reply, gallery_id, post_id, reply=True)
    title = " ".join(primary[0].text().split())
    if not title or any(node.tag in _INERT for node in titles[0].elements()):
        raise BoardParseError("글 제목을 확정할 수 없음")
    kind, uid = _author(writers)
    return Post(gallery_id, post_id, title, kind, uid)


def parse_board(html: str, gallery_id: str) -> tuple[Post, ...]:
    """Return verified post identities; raise rather than guess a page identity.

    Supported links are HTTPS DCInside minor view links and root-relative minor
    view links. Rows must be explicitly closed. Nicknames and IPs are discarded.
    """
    return tuple(entry.post for entry in parse_board_entries(html, gallery_id))


def parse_board_entries(html: str, gallery_id: str, *, reject_special_ids=()) -> tuple[ListEntry, ...]:
    """Same validated rows, with list-only ordinary/recommended/unknown state."""
    entries, excluded_ids = parse_board_listing(html, gallery_id)
    if excluded_ids.intersection(str(value) for value in reject_special_ids):
        raise BoardParseError('선택한 글이 제외 대상 행으로 변경됨')
    return entries


def parse_board_listing(html: str, gallery_id: str) -> tuple[tuple[ListEntry, ...], frozenset[str]]:
    """Parse once, retaining excluded-row IDs for cross-page notice checks.

    The caller checks the union against candidates only after reading all pages.
    IDs remain exact strings, matching the existing reject_special_ids contract.
    """
    validate_gallery(gallery_id)
    if not isinstance(html, str):
        raise BoardParseError("게시판 HTML은 문자열이어야 함")
    parser = _ListHTML()
    parser.feed(html)
    parser.close()
    if len(parser.tables) != 1 or parser.stack or parser.invalid:
        raise BoardParseError("닫힌 gall_list 표 한 개를 확인할 수 없음")
    table = parser.tables[0]
    if table.bad_attrs:
        raise BoardParseError("목록 표 속성이 중복됨")
    entries: list[ListEntry] = []
    seen: set[int] = set()
    excluded_ids: set[str] = set()
    for row in table.elements("tr"):
        post = _parse_row(row, gallery_id)
        if post is None and row.attrs.get('data-no') is not None:
            excluded_ids.add(row.attrs['data-no'])
        if post is not None:
            if post.post_id in seen:
                raise BoardParseError("동일 글 번호가 여러 일반 행에 나타남")
            seen.add(post.post_id)
            entries.append(ListEntry(post, _post_kind(row), _new_member_badge(row, post)))
    return tuple(entries), frozenset(excluded_ids)
