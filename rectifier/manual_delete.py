"""Validation of a manual selection before any delete request is built."""
from .models import validate_gallery, validate_post_id
from .policy import MAX_MANUAL_TARGETS, validate_manual_pages
from .selection import SelectionReview


def validate_review(review):
    if not isinstance(review, SelectionReview):
        raise ValueError('수동 선택 자료 오류')
    validate_gallery(review.gallery_id)
    if not review.complete:
        raise ValueError('지정한 페이지를 모두 조회한 뒤 선택해야 함')
    validate_manual_pages(review.pages)
    if not 1 <= len(review.rows) <= MAX_MANUAL_TARGETS:
        raise ValueError(f'일반글 1~{MAX_MANUAL_TARGETS}개를 선택해야 함')
    ids = tuple(validate_post_id(row.post_id) for row in review.rows)
    if len(set(ids)) != len(ids):
        raise ValueError('선택한 글 번호 중복')
    if any(row.disposition not in {'delete_guest', 'protected_member'} or row.post_kind != 'ordinary'
           for row in review.rows):
        raise ValueError('일반글만 지원함 · 개념글/공지/작성자 또는 종류 불명은 제외해야 함')
