"""Read the selected article's header/body, never its neighbouring list rows."""
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
import re

from .models import Post, validate_gallery, validate_post_id
from .parser import _Node, _VOID, _INERT, _author


class ViewError(ValueError):
    pass


@dataclass(frozen=True)
class PostSnapshot:
    post: Post
    created_at: str
    text: str
    media_count: int
    comment_count: int
    gallery_type: str
    images: tuple[str, ...] = ()

    @property
    def fingerprint(self):
        data = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        return sha256(data.encode('utf-8')).hexdigest()


class _View(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.roots = []
        self.stack = []
        self.inert = []
        self.fields = {}
        self.invalid = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        node = _Node(tag, a, len(a) != len(attrs))
        if not self.stack:
            if tag in _INERT:
                self.inert.append(tag)
            if self.inert:
                return
            key = a.get('id')
            if tag == 'input' and key in {'gallery_id', 'no', '_GALLTYPE_'}:
                self.fields.setdefault(key, []).append(None if node.bad_attrs else a.get('value'))
            if tag == 'div' and node.classes & {'gallview_head', 'writing_view_box'}:
                self.roots.append(node)
                self.stack.append(node)
            return
        if len(self.stack) > 64:
            raise ViewError('본문 중첩 제한 초과')
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if not self.stack:
            if self.inert and self.inert[-1] == tag:
                self.inert.pop()
            return
        if tag in _VOID:
            return
        # Official guest headers contain one stray </span> after the nickname.
        # An end tag with no matching open element cannot change our boundaries.
        if not any(n.tag == tag for n in self.stack):
            return
        if self.stack[-1].tag != tag:
            self.invalid = True
            return
        self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)


def _one(nodes):
    values = list(nodes)
    if len(values) != 1 or values[0].bad_attrs:
        raise ViewError('본문 필드가 없거나 중복됨')
    return values[0]


def parse_post_view(html, gallery_id, post_id):
    validate_gallery(gallery_id)
    validate_post_id(post_id)
    if not isinstance(html, str):
        raise ViewError('본문 HTML 형식 오류')
    parsed = _View(html)
    if parsed.invalid or parsed.stack:
        raise ViewError('닫힌 본문 구조를 확인하지 못함')
    expected = {'gallery_id': gallery_id, 'no': str(post_id), '_GALLTYPE_': 'M'}
    for key, value in expected.items():
        values = parsed.fields.get(key, [])
        # The official page repeats _GALLTYPE_ in the comment form.
        if not values or any(item != value for item in values) or (key != '_GALLTYPE_' and len(values) != 1):
            raise ViewError('본문의 갤러리·글 번호·유형이 일치하지 않음')
    head = _one(n for n in parsed.roots if 'gallview_head' in n.classes)
    body = _one(n for n in parsed.roots if 'writing_view_box' in n.classes)
    title = _one(n for n in head.elements() if 'title_subject' in n.classes).text().strip()
    date_node = _one(n for n in head.elements() if 'gall_date' in n.classes)
    date = date_node.text().strip()
    try:
        created = datetime.strptime(date, '%Y.%m.%d %H:%M:%S')
    except (ValueError, TypeError):
        raise ViewError('작성 시각을 확인하지 못함') from None
    if date_node.attrs.get('title') not in (None, created.strftime('%Y-%m-%d %H:%M:%S')):
        raise ViewError('작성 시각의 표시와 속성이 다름')
    reply = _one(n for n in head.elements() if 'gall_comment' in n.classes).text().strip()
    match = re.fullmatch(r'댓글\s*([0-9]+)', reply)
    if not title or not match:
        raise ViewError('제목·댓글 수를 확인하지 못함')
    kind, uid = _author([n for n in head.elements() if 'gall_writer' in n.classes])
    # Count all media/links rather than claim a text archive preserves attachments.
    media = sum(n.tag in {'img', 'video', 'audio', 'iframe', 'object', 'embed', 'a', 'source'}
                for n in body.elements())
    images = tuple(n.attrs.get('src') or '' for n in body.elements() if n.tag == 'img')
    return PostSnapshot(Post(gallery_id, post_id, title, kind, uid), created.isoformat(),
                        body.text().strip(), media, int(match[1]), 'M', images)
