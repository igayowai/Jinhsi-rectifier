"""Human-entered image challenge support; no OCR, browser import or persistence."""
import base64
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
import struct
from urllib.parse import urljoin, urlsplit


class CaptchaError(ValueError):
    pass


@dataclass(frozen=True)
class Captcha:
    source: str = field(repr=False)
    rand_code: str = field(repr=False)


def validate_image(data):
    if not isinstance(data, bytes) or not 10 <= len(data) <= 1_000_000:
        raise CaptchaError('보안문자 이미지 크기 또는 형식 오류')
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24 and data[12:16] == b'IHDR':
        width, height = struct.unpack('>II', data[16:24])
    elif data.startswith((b'GIF87a', b'GIF89a')):
        width, height = struct.unpack('<HH', data[6:10])
    else:
        raise CaptchaError('현재 보안문자 이미지는 PNG/GIF만 지원함')
    if not (0 < width <= 1600 and 0 < height <= 800):
        raise CaptchaError('보안문자 이미지 해상도 제한 초과')
    return data


def read_image(session, source):
    if not isinstance(source, str) or len(source) > 1_400_000:
        raise CaptchaError('보안문자 이미지 주소 형식 오류')
    if source.startswith(('data:image/png;base64,', 'data:image/gif;base64,')):
        try:
            return validate_image(base64.b64decode(source.split(',', 1)[1], validate=True))
        except (ValueError, TypeError):
            raise CaptchaError('보안문자 이미지 해석 실패') from None
    try:
        url = urljoin('https://msign.dcinside.com/login', source)
        u = urlsplit(url)
        if (u.scheme != 'https' or u.hostname != 'msign.dcinside.com' or u.username is not None
                or u.password is not None or u.port not in (None, 443) or u.fragment
                or any(ord(c) < 32 or c == '\\' for c in source)
                or not re.fullmatch(r'/[A-Za-z0-9_./-]*captcha[A-Za-z0-9_./-]*', u.path, re.I)
                or '..' in u.path or len(url) > 4096):
            raise ValueError
    except ValueError:
        raise CaptchaError('확인되지 않은 보안문자 이미지 주소임') from None
    response = session.request('GET', url, headers={'Referer': 'https://msign.dcinside.com/login'}, binary=True)
    if response.status != 200:
        raise CaptchaError('보안문자 이미지 조회 실패; 재시도하지 않음')
    return validate_image(response.body)


class _CaptchaForm(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.form = None
        self.found = []
        self.inert = []
        self.invalid = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.inert.append(tag)
        if self.inert:
            return
        a = dict(attrs)
        if self.form is not None and len(a) != len(attrs):
            self.invalid = True
        if tag == 'form':
            if self.form is not None or len(a) != len(attrs):
                self.invalid = True
            self.form = {'action': a.get('action', ''), 'method': a.get('method', '').lower(),
                         'inputs': {}, 'images': []}
        if self.form is None:
            return
        if tag == 'input' and a.get('id') in {'code', 'password', 'conKey', 'randCode', 'captcha_code'}:
            key = a['id']
            if key in self.form['inputs']:
                self.invalid = True
            self.form['inputs'][key] = a
        if tag == 'img' and a.get('id') == 'captchaCode':
            self.form['images'].append(a.get('src', ''))

    def handle_endtag(self, tag):
        if self.inert:
            if tag == self.inert[-1]:
                self.inert.pop()
            return
        if tag == 'form' and self.form is not None:
            self.found.append(self.form)
            self.form = None


def from_html(html):
    """Recognize only the observed loginRequest selectors and a closed POST form."""
    parsed = _CaptchaForm(html)
    if parsed.invalid or parsed.form is not None:
        return None
    candidates = []
    for form in parsed.found:
        inputs = form['inputs']
        if not {'code', 'password', 'conKey', 'randCode', 'captcha_code'} <= inputs.keys():
            continue
        if (form['method'] != 'post'
                or urljoin('https://msign.dcinside.com/login', form['action']) != 'https://msign.dcinside.com/login'
                or len(form['images']) != 1 or not form['images'][0]
                or inputs['captcha_code'].get('name') != 'captchaCode'
                or 'disabled' in inputs['captcha_code'] or 'disabled' in inputs['randCode']):
            continue
        rand = inputs['randCode'].get('value')
        if isinstance(rand, str) and 0 < len(rand) <= 1024:
            candidates.append(Captcha(form['images'][0], rand))
    return candidates[0] if len(candidates) == 1 else None


def from_preflight(data):
    if (isinstance(data, dict) and data.get('reason') == 'limit_login' and data.get('result') is not True
            and isinstance(data.get('code'), str) and data['code']
            and isinstance(data.get('randCode'), str) and 0 < len(data['randCode']) <= 1024):
        return Captcha(data['code'], data['randCode'])
    return None


def validate_answer(value):
    if value is None:
        raise CaptchaError('사용자가 보안문자 입력을 취소했거나 시간이 초과됨')
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,16}', value.strip()):
        raise CaptchaError('보안문자 입력 형식 오류')
    return value.strip()
