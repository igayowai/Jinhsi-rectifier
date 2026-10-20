"""Delete endpoint and strict interpretation of its JSON acknowledgement.

Official reference: /_js/minor_gall_manager.js, del_list (2026-09-15).
result='success' is the official action acknowledgement, not proof per article.
No quota guesses, redirects or automatic resubmission.
"""
import json

from .models import Outcome, OutcomeCode


DELETE_URL = 'https://gall.dcinside.com/ajax/minor_manager_board_ajax/delete_list'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    # Python's default JSON parser accepts NaN/Infinity; the site's JSON parser
    # does not. Never include the rejected value in errors or diagnostics.
    raise ValueError('nonstandard JSON constant')


def _assess_response(response):
    """One source for the decision and its secret-free diagnostic reason."""
    mime = response.header('Content-Type').split(';')[0].strip().lower()
    kind = {'application/json': 'json', 'text/html': 'html', 'text/plain': 'plain', '': 'missing'}.get(mime, 'other')
    output = {'http_status': response.status, 'content_type_kind': kind,
              'valid_json': False, 'json_object': False, 'result_kind': 'unread',
              'unknown_field_count': None, 'message_type_supported': None,
              'decision_reason': 'invalid_json'}
    data = None
    try:
        data = json.loads(response.body, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (ValueError, TypeError):
        pass
    else:
        output['valid_json'] = True
    output['json_object'] = isinstance(data, dict)
    if isinstance(data, dict):
        output['result_kind'] = 'success' if data.get('result') == 'success' else 'other' if 'result' in data else 'missing'
        output['unknown_field_count'] = len(set(data) - {'result', 'msg'})
        output['message_type_supported'] = 'msg' not in data or isinstance(data['msg'], (str, type(None)))
    code = OutcomeCode.UNKNOWN
    if response.status in {401, 403, 429}:
        code = {401: OutcomeCode.AUTH_REQUIRED, 403: OutcomeCode.FORBIDDEN,
                429: OutcomeCode.RATE_LIMITED}[response.status]
        output['decision_reason'] = 'http_' + code.value
    elif response.status != 200:
        output['decision_reason'] = 'http_status_unverified'
    elif not output['valid_json']:
        output['decision_reason'] = 'invalid_json'
    elif not output['json_object']:
        output['decision_reason'] = 'json_not_object'
    elif output['result_kind'] != 'success':
        output['decision_reason'] = 'result_not_success'
    elif output['unknown_field_count']:
        # No currently verified contract for extra success/partial-count fields.
        output['decision_reason'] = 'unsupported_fields'
    elif not output['message_type_supported']:
        output['decision_reason'] = 'unsupported_message_type'
    else:
        # Official del_list explicitly requests dataType='json'. jQuery ignores
        # the response MIME header in that mode; strict JSON + result still apply.
        code = OutcomeCode.SUCCESS
        output['decision_reason'] = 'single_success_json'
    return code, output


def response_diagnostics(response):
    """Fixed labels/counts only; never response values, messages or unknown keys."""
    return _assess_response(response)[1]


def deletion_outcome(response, post_id):
    code, _ = _assess_response(response)
    return Outcome(code, (post_id,) if code is OutcomeCode.SUCCESS else ())
