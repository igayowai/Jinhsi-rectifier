from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from .models import AuthorKind, Plan, PlanItem, Post, validate_gallery, validate_post_id


MAX_TARGETS = 3  # First-phase bound; this is NOT the site's daily quota.
MAX_MANUAL_TARGETS = 250  # Local manual-selection bound, NOT a server/quota claim.


def validate_manual_pages(pages):
    if (not isinstance(pages, (tuple, list)) or not 1 <= len(pages) <= 5
            or any(type(p) is not int or not 1 <= p <= 1_000_000 for p in pages)
            or len(set(pages)) != len(pages)):
        raise ValueError('서로 다른 목록 페이지 1~5개 필요함')
    return tuple(pages)


class DeletePolicy(StrEnum):
    GUEST_ONLY = 'guest_only'
    MANUAL_SELECTED = 'manual_selected'


def permits_author(post, policy):
    if not isinstance(policy, DeletePolicy):
        raise ValueError('삭제 정책 오류')
    return (isinstance(post, Post) and (
        (post.author_kind is AuthorKind.GUEST and not post.uid)
        or (policy is DeletePolicy.MANUAL_SELECTED
            and post.author_kind is AuthorKind.MEMBER and bool(post.uid))))


@dataclass(frozen=True)
class ManualPlan:
    """Explicit human selection; intentionally NOT a guest Plan subclass."""
    gallery_id: str
    account_id: str
    items: tuple[PlanItem, ...]


def build_manual_plan(gallery_id, account_id, posts):
    posts = tuple(posts)
    validate_gallery(gallery_id)
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError('예상 계정이 필요함')
    if (not 1 <= len(posts) <= MAX_MANUAL_TARGETS
            or any(not permits_author(p, DeletePolicy.MANUAL_SELECTED)
                   or p.gallery_id != gallery_id for p in posts)
            or len({p.post_id for p in posts}) != len(posts)):
        raise ValueError('수동 선택의 갤러리·작성자·고정 번호 오류')
    return ManualPlan(gallery_id, account_id, tuple(
        PlanItem(p.post_id, 'delete_manual', p.title) for p in posts))


def build_plan(gallery_id: str, account_id: str, requested_ids, posts) -> Plan:
    validate_gallery(gallery_id)
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("예상 로그인 ID가 필요함")
    ids = tuple(dict.fromkeys(validate_post_id(value) for value in requested_ids))
    if not 1 <= len(ids) <= MAX_TARGETS:
        raise ValueError(f"첫 단계에서는 고정 글 번호 1~{MAX_TARGETS}개만 지정 가능함")
    return Plan(gallery_id, account_id.strip(), _classify_posts(gallery_id, posts, ids))


def preview_page_items(gallery_id: str, posts) -> tuple[PlanItem, ...]:
    """Classify one parsed page without creating an executable Plan."""
    validate_gallery(gallery_id)
    return _classify_posts(gallery_id, posts)


def _classify_posts(gallery_id, posts, ids=None):
    grouped = defaultdict(list)
    for post in posts:
        if not isinstance(post, Post) or post.gallery_id != gallery_id:
            raise ValueError("다른 갤러리 또는 잘못된 게시글 자료")
        grouped[post.post_id].append(post)
    items = []
    for post_id in grouped if ids is None else ids:
        candidates = grouped[post_id]
        if not candidates:
            items.append(PlanItem(post_id, "not_observed"))
            continue
        post = candidates[0]
        if any(candidate != post for candidate in candidates):
            disposition = "conflicting_metadata"
        elif post.author_kind is AuthorKind.MEMBER:
            disposition = "protected_member"
        elif post.author_kind is AuthorKind.GUEST:
            disposition = "delete_guest"
        else:
            disposition = "unknown_author"
        items.append(PlanItem(post_id, disposition, post.title))
    return tuple(items)
