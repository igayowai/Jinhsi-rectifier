"""Bounded anonymous multi-page preview, without an executable deletion plan."""
from collections import Counter, defaultdict
from dataclasses import asdict
from time import perf_counter

from .models import validate_gallery
from .parser import BoardParseError, ListEntry, PostKind, parse_board_entries
from .policy import preview_page_items
from .public_board import PublicReadError, read_public_board


MAX_PREVIEW_PAGES = 5  # Prototype read bound, not a server limit.


def classify_list_entries(gallery_id, entries, *, include_recommended=False, include_new_members=False):
    """Annotate preview candidates; this does not construct an execution Plan."""
    if type(include_recommended) is not bool:
        raise ValueError('개념글 포함 설정 오류')
    if type(include_new_members) is not bool:
        raise ValueError('신규 회원 포함 설정 오류')
    entries = tuple(entries)
    if any(not isinstance(entry, ListEntry) or not isinstance(entry.post_kind, PostKind)
           for entry in entries):
        raise ValueError('목록 상태 자료 오류')
    items = preview_page_items(gallery_id, (entry.post for entry in entries))
    kinds = defaultdict(set)
    badges = defaultdict(set)
    for entry in entries:
        kinds[entry.post.post_id].add(entry.post_kind)
        badges[entry.post.post_id].add(entry.new_member_badge and bool(entry.post.uid))
    output = []
    for item in items:
        observed = kinds[item.post_id]
        kind = next(iter(observed)).value if len(observed) == 1 else 'conflicting'
        disposition = item.disposition
        if (include_new_members and disposition == 'protected_member'
                and badges[item.post_id] == {True}):
            disposition = 'delete_new_member'
        if disposition not in ('delete_guest', 'delete_new_member'):
            batch_reason = disposition
        else:
            batch_reason = {'ordinary': 'candidate',
                            'recommended': 'candidate' if include_recommended else 'single_only',
                            'unknown': 'unknown_post_kind',
                            'conflicting': 'post_kind_conflict'}[kind]
        output.append({**asdict(item), 'disposition': disposition,
                       'post_kind': kind, 'batch_disposition': batch_reason})
    counts = Counter(item['disposition'] for item in output)
    batch_counts = Counter(item['batch_disposition'] for item in output)
    count_keys = ('delete_guest', 'protected_member', 'unknown_author', 'conflicting_metadata')
    if include_new_members:
        count_keys += ('delete_new_member',)
    return {'items': output, 'observed_count': len(output),
            'counts': {key: counts[key] for key in count_keys},
            'target_count': counts['delete_guest'] + counts['delete_new_member'],
            'batch_candidate_count': batch_counts['candidate'],
            'batch_counts': {key: batch_counts[key] for key in (
                'candidate', 'single_only', 'unknown_post_kind', 'post_kind_conflict',
                'protected_member', 'unknown_author', 'conflicting_metadata')}}


def preview_pages(gallery_id, account_id, pages, *, reader=None):
    validate_gallery(gallery_id)
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError('예상 로그인 ID가 필요함')
    pages = tuple(pages)
    if not 1 <= len(pages) <= MAX_PREVIEW_PAGES:
        raise ValueError('미리보기는 페이지 1~5개만 지정 가능함')
    if any(type(page) is not int or not 1 <= page <= 1_000_000 for page in pages):
        raise ValueError('페이지 번호 오류')
    if len(set(pages)) != len(pages):
        raise ValueError('같은 페이지를 중복 지정할 수 없음')
    reader = read_public_board if reader is None else reader
    entries, page_results = [], []
    stopped = None
    started = perf_counter()
    for page in pages:
        if stopped is not None:
            page_results.append({'page': page, 'status': 'not_requested'})
            continue
        page_started = perf_counter()
        try:
            html = reader(gallery_id, page)
            parsed = parse_board_entries(html, gallery_id)
        except PublicReadError:
            stopped = 'public_read_failed'
        except BoardParseError:
            stopped = 'invalid_board'
        if stopped is not None:
            page_results.append({'page': page, 'status': 'failed', 'reason': stopped,
                                 'elapsed_ms': round((perf_counter() - page_started) * 1000, 3)})
        else:
            entries.extend(parsed)
            page_results.append({'page': page, 'status': 'read', 'row_count': len(parsed),
                                 'elapsed_ms': round((perf_counter() - page_started) * 1000, 3)})
    classified = classify_list_entries(gallery_id, entries)
    return {'mode': 'preview_only', 'scope': 'all_on_pages',
            'gallery_id': gallery_id, 'account_id': account_id.strip(),
            'source': 'public_pages', 'requested_pages': list(pages),
            'page_results': page_results,
            'requested_pages_completed': stopped is None, 'stopped': stopped,
            'coverage': 'not_guaranteed',
            'elapsed_ms': round((perf_counter() - started) * 1000, 3),
            **classified, 'observed_row_count': len(entries),
            'duplicate_observations': len(entries) - classified['observed_count'],
            'authenticated': False, 'live_delete_available': False, 'delete_requests': 0}
