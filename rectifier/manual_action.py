"""Current-list manual deletion: one consumed action, no content downloads.

No historical journal is required to send or retry from a newly selected list.
The caller owns an exclusive authenticated session for the entire action.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event, Lock

from .batch_journal import BatchResponse
from .defense_runner import ListDefenseAdapter, DefenseDispatch, DefenseReadError, DispatchReceipt
from .manual_delete import validate_review
from .page_preview import classify_list_entries
from .policy import DeletePolicy
from .performance import ManualPerformance
from .request_budget import RequestBudget
from .diagnostics import record_error


@dataclass(frozen=True)
class ManualPart:
    post_ids: tuple[int, ...] = field(repr=False)
    receipt: DispatchReceipt

    @property
    def category(self):
        if not self.receipt.attempted:
            return 'not_sent'
        return {BatchResponse.SUCCESS_REPORTED: 'accepted',
                BatchResponse.REJECTION_REPORTED: 'rejected'}.get(self.receipt.response, 'unknown')


@dataclass(frozen=True)
class ManualReceipt(DispatchReceipt):
    parts: tuple[ManualPart, ...] = field(default=(), repr=False)

    @property
    def result_counts(self):
        counts = dict(accepted=0, rejected=0, unknown=0, not_sent=0)
        for part in self.parts:
            counts[part.category] += len(part.post_ids)
        return counts


def format_manual_result(receipt):
    c = receipt.result_counts
    text = (f"수락 {c['accepted']}개 · 거부 {c['rejected']}개 · 미확인 {c['unknown']}개 · 미전송 {c['not_sent']}개"
            '\n서버 응답에 따른 묶음별 분류이며 글별 삭제 완료 확인은 아님.')
    for category, label in (('rejected', '거부'), ('unknown', '미확인'), ('not_sent', '미전송')):
        ids = [str(pid) for part in receipt.parts if part.category == category for pid in part.post_ids]
        if ids:
            text += f'\n{label} 글 번호: ' + ', '.join(ids)
    return text


class ManualAction:
    def __init__(self, login_result, review):
        validate_review(review)
        self.review = review
        self.adapter = ListDefenseAdapter(login_result, review.gallery_id, pages=review.pages,
                                           max_age=60, policy=DeletePolicy.MANUAL_SELECTED,
                                           request_budget=RequestBudget(30))
        self._lock = Lock()
        self._used = False
        self.performance = ManualPerformance(len(review.rows), len(review.pages))
        self.adapter.performance = self.performance

    def execute(self, cancel):
        if not self._lock.acquire(blocking=False):
            raise DefenseReadError('already_used')
        receipt, reason, started = None, None, False
        try:
            if self._used:
                raise DefenseReadError('already_used')
            self._used = True  # Even ambiguous responses require a new selection.
            started = True
            self.performance.start()
            if cancel.is_set():
                raise DefenseReadError('user_stopped')
            with self.performance.stage('snapshot'):
                snapshot = self.adapter.read_snapshot(cancel)
            with self.performance.stage('selection'):
                current = {item['post_id']: item for item in
                           classify_list_entries(self.review.gallery_id, snapshot.entries)['items']}
                for row in self.review.rows:
                    item = current.get(row.post_id)
                    if (item is None or item['title'] != row.title
                            or item['disposition'] != row.disposition
                            or item['post_kind'] != row.post_kind):
                        raise DefenseReadError('selection_changed')
            ids = tuple(row.post_id for row in self.review.rows)
            receipt = self._send_selected(ids, snapshot, cancel)
            return receipt
        except DefenseReadError as error:
            reason = str(error)
            raise
        except Exception:
            record_error('manual_action.execute')
            reason = 'preparation_failed'
            raise
        finally:
            if started:
                self.performance.complete(receipt, reason)
            self._lock.release()

    def _send_selected(self, ids, snapshot, cancel):
        reserved = DefenseDispatch(self.review.gallery_id, self.adapter.account, ids)
        groups = ((self.adapter, reserved, snapshot),)
        # Two bounded levels reuse the existing consumed-snapshot contract.
        # Up to five groups cover 250 selected IDs; each is at most 60 IDs.
        if len(ids) > 60:
            groups = self.adapter.partition(reserved, snapshot, cancel,
                workers=(len(ids)+59)//60, manual_selected=True)
        children = []
        for adapter, dispatch, authority in groups:
            if len(dispatch.post_ids) <= 5:
                children.append((adapter, dispatch, authority))
            else:
                children.extend(adapter.partition(dispatch, authority, cancel,
                    workers=12, max_part_size=5, manual_selected=True))
        coverage = [pid for _, dispatch, _ in children for pid in dispatch.post_ids]
        if len(coverage) != len(ids) or set(coverage) != set(ids):
            raise DefenseReadError('scope_mismatch')
        abort = Event()

        class SendCancel:
            def is_set(self):
                return cancel.is_set() or abort.is_set()

        def send(child):
            adapter, dispatch, authority = child
            try:
                outcome = adapter.send(dispatch, authority, SendCancel())
                if not isinstance(outcome, DispatchReceipt):
                    raise TypeError('invalid receipt')
            except Exception:
                record_error('manual_action.send')
                # Unexpected failure might follow transmission; never label it unsent.
                outcome = DispatchReceipt(BatchResponse.UNKNOWN, True, 'read_failed')
            if outcome.stopped or outcome.response is not BatchResponse.SUCCESS_REPORTED:
                abort.set()
            return ManualPart(dispatch.post_ids, outcome)

        # Preinitialize unscheduled parts; executor submission failure cannot
        # discard successes from requests that already started.
        parts = [ManualPart(d.post_ids, DispatchReceipt(BatchResponse.UNKNOWN, False))
                 for _, d, _ in children]
        failure = None
        if len(children) == 1:
            parts[0] = send(children[0])
        else:
            try:
                with ThreadPoolExecutor(max_workers=min(12, len(children)),
                                        thread_name_prefix='rectifier-manual') as pool:
                    futures = {}
                    for index, child in enumerate(children):
                        if SendCancel().is_set():
                            break
                        try:
                            futures[pool.submit(send, child)] = index
                        except Exception:
                            record_error('manual_action.send')
                            abort.set()
                            failure = 'read_failed'
                            break
                    for future in as_completed(futures):
                        index = futures[future]
                        try:
                            parts[index] = future.result()
                        except Exception:
                            record_error('manual_action.send')
                            abort.set()
                            failure = 'read_failed'
                            parts[index] = ManualPart(children[index][1].post_ids,
                                DispatchReceipt(BatchResponse.UNKNOWN, True, 'read_failed'))
            except Exception:
                record_error('manual_action.send')
                abort.set()
                failure = 'read_failed'
        # Prefer a concrete rejection over sibling cancellation/ambiguous response.
        reason = next((p.receipt.stopped for p in parts if p.receipt.stopped
                       and p.receipt.stopped != 'user_stopped'), None) or failure
        if reason is None:
            reason = ('user_stopped' if cancel.is_set() else
                      'response_unknown' if any(p.category == 'unknown' for p in parts) else
                      'user_stopped' if abort.is_set() else None)
        response = (BatchResponse.SUCCESS_REPORTED if all(p.category == 'accepted' for p in parts)
                    else BatchResponse.UNKNOWN if any(p.category == 'unknown' for p in parts)
                    else BatchResponse.REJECTION_REPORTED if any(p.category == 'rejected' for p in parts)
                    else BatchResponse.UNKNOWN)
        return ManualReceipt(response, any(p.receipt.attempted for p in parts), reason, tuple(parts))
