from dataclasses import dataclass
from enum import StrEnum
import re


class AuthorKind(StrEnum):
    GUEST = "guest"
    MEMBER = "member"
    UNKNOWN = "unknown"


def validate_gallery(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value):
        raise ValueError("잘못된 갤러리 ID")
    return value


def validate_post_id(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("글 번호는 양의 정수여야 함")
    return value


@dataclass(frozen=True)
class Post:
    gallery_id: str
    post_id: int
    title: str
    author_kind: AuthorKind
    uid: str = ""

    def __post_init__(self):
        validate_gallery(self.gallery_id)
        validate_post_id(self.post_id)
        if not isinstance(self.author_kind, AuthorKind):
            raise ValueError("작성자 유형 오류")
        if self.author_kind is AuthorKind.GUEST and self.uid:
            raise ValueError("회원 UID가 있는 글을 비회원으로 지정할 수 없음")


@dataclass(frozen=True)
class Identity:
    account_id: str
    gallery_id: str
    can_delete: bool


class OutcomeCode(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth_required"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Outcome:
    code: OutcomeCode
    # Only an explicit, verified server acknowledgement may populate these.
    confirmed_ids: tuple[int, ...] = ()

    def __post_init__(self):
        if not isinstance(self.code, OutcomeCode) or type(self.confirmed_ids) is not tuple:
            raise ValueError("응답 계약 오류")
        for post_id in self.confirmed_ids:
            validate_post_id(post_id)
        if len(set(self.confirmed_ids)) != len(self.confirmed_ids):
            raise ValueError("중복 성공 번호")


@dataclass(frozen=True)
class PlanItem:
    post_id: int
    disposition: str
    title: str = ""


@dataclass(frozen=True)
class Plan:
    gallery_id: str
    account_id: str
    items: tuple[PlanItem, ...]

    @property
    def targets(self) -> tuple[int, ...]:
        return tuple(item.post_id for item in self.items if item.disposition == "delete_guest")
