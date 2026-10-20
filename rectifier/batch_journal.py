"""Batch delete response labels and target validation shared by manual and defense deletes.

A response never proves individual deletions; callers re-read the list.
"""
from enum import StrEnum
import re

from .models import validate_gallery, validate_post_id
from .policy import MAX_TARGETS, MAX_MANUAL_TARGETS


class BatchResponse(StrEnum):
    SUCCESS_REPORTED = 'success_reported'
    REJECTION_REPORTED = 'rejection_reported'
    UNKNOWN = 'unknown'


def _validate_targets(gallery, account, post_ids, *, max_targets=MAX_TARGETS):
    validate_gallery(gallery)
    if not isinstance(account, str) or not re.fullmatch(r'[a-z0-9_-]{3,64}', account):
        raise ValueError('묶음 계정 오류')
    if (type(max_targets) is not int or max_targets not in {MAX_TARGETS, MAX_MANUAL_TARGETS}
            or not isinstance(post_ids, (tuple, list)) or not 1 <= len(post_ids) <= max_targets):
        raise ValueError('묶음 대상 범위 오류')
    ids = tuple(validate_post_id(value) for value in post_ids)
    if len(set(ids)) != len(ids):
        raise ValueError('묶음 대상 중복')
    return ids
