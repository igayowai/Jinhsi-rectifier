"""Post view identity parsing and delete-response assessment (moved from the retired trial tests)."""
import json
from unittest import TestCase
from unittest.mock import patch

from rectifier.demo import DEMO_HTML
from rectifier.live import deletion_outcome, response_diagnostics
from rectifier.models import AuthorKind, OutcomeCode
from rectifier.post_view import parse_post_view
from tests.common import VIEW, response


class ResponseParsingTests(TestCase):
    def setUp(self):
        self.no_network = patch('socket.socket', side_effect=AssertionError('offline only'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.snapshot = parse_post_view(VIEW, 'mnt', 647172)

    def test_selected_article_ignores_neighbouring_members(self):
        parsed = parse_post_view(VIEW + DEMO_HTML, 'mnt', 647172)
        self.assertEqual(parsed.post.author_kind, AuthorKind.GUEST)
        self.assertEqual(parsed.comment_count, 0)
        self.assertNotIn('192.0.2.1', str(parsed))

    def test_member_and_unknown_are_never_guest(self):
        for html, expected in (
            (VIEW.replace('data-uid="" data-ip="192.0.2.1"', 'data-uid="member" data-ip=""'), AuthorKind.MEMBER),
            (VIEW.replace('data-uid="" ', ''), AuthorKind.UNKNOWN),
            (VIEW.replace('data-uid=""', 'data-uid="" data-uid="member"'), AuthorKind.UNKNOWN)):
            with self.subTest(expected=expected):
                self.assertEqual(parse_post_view(html, 'mnt', 647172).post.author_kind, expected)

    def test_malformed_and_conflicting_identity_rejected(self):
        variants = [VIEW.replace('value="647172"', 'value="647173"'),
                    VIEW.replace('value="mnt"', 'value="other"'),
                    VIEW.replace('value="M"', 'value="MI"'),
                    VIEW + '<input id="no" value="647172">',
                    VIEW.replace('</div></div>', '</div>'),
                    VIEW.replace('댓글 0', '댓글 ?'),
                    VIEW.replace('title="2023-11-23 18:42:52"', 'title="2024-01-01 00:00:00"')]
        for html in variants:
            with self.subTest(html=html[-70:]), self.assertRaises(ValueError):
                parse_post_view(html, 'mnt', 647172)

    def test_official_stray_span_and_repeated_type_are_supported(self):
        html = VIEW.replace('<span class="gall_date"', '</span><span class="gall_date"')
        html += '<input id="_GALLTYPE_" value="M">'
        self.assertEqual(parse_post_view(html, 'mnt', 647172), self.snapshot)

    def test_images_and_body_changes_change_fingerprint(self):
        for changed in (VIEW.replace('synthetic text', 'new text'),
                        VIEW.replace('댓글 0', '댓글 1'),
                        VIEW.replace('synthetic text', '<img src="https://example.invalid/image">')):
            self.assertNotEqual(parse_post_view(changed, 'mnt', 647172).fingerprint, self.snapshot.fingerprint)

    def test_only_explicit_single_success_is_confirmed(self):
        for body in ('{}', '[]', '{"result":true}', '{"result":"failed"}', '<html>login</html>',
                     '{"result":"success","deleted_ids":[999]}',
                     '{"result":"failed","result":"success"}',
                     '{"result":"success","count":0}'):
            with self.subTest(body=body):
                result = deletion_outcome(response(body, content_type='application/json'), 647172)
                self.assertEqual(result.code, OutcomeCode.UNKNOWN)
                self.assertEqual(result.confirmed_ids, ())
        for status in (302, 401, 403, 429, 500):
            self.assertNotEqual(deletion_outcome(response('{"result":"success"}', status,
                'application/json'), 647172).code, OutcomeCode.SUCCESS)
        self.assertEqual(deletion_outcome(response('{"result":"success"}'), 647172).code, OutcomeCode.SUCCESS)

    def test_official_json_mode_accepts_body_independently_of_mime_header(self):
        for mime in ('application/json', 'text/html; charset=UTF-8', 'text/plain',
                     'application/octet-stream', ''):
            with self.subTest(mime=mime):
                reply = response('{"result":"success","msg":"synthetic"}', content_type=mime)
                outcome = deletion_outcome(reply, 647172)
                self.assertEqual(outcome.code, OutcomeCode.SUCCESS)
                self.assertEqual(outcome.confirmed_ids, (647172,))
                self.assertEqual(response_diagnostics(reply)['decision_reason'], 'single_success_json')

    def test_strict_json_and_failure_reasons_are_shared_with_diagnostics(self):
        for body, reason in (
            ('', 'invalid_json'), ('<html>login</html>', 'invalid_json'),
            ('{"result":"success"} trailing', 'invalid_json'),
            ('{"result":"success","msg":NaN}', 'invalid_json'),
            ('{"result":"success","msg":Infinity}', 'invalid_json'),
            ('{"result":"success","result":"success"}', 'invalid_json'),
            ('null', 'json_not_object'), ('["success"]', 'json_not_object'),
            ('{}', 'result_not_success'), ('{"result":true}', 'result_not_success'),
            ('{"result":"failed"}', 'result_not_success'),
            ('{"result":"success","count":0}', 'unsupported_fields'),
            ('{"result":"success","msg":[]}', 'unsupported_message_type')):
            for mime in ('text/html', 'application/json'):
                with self.subTest(body=body, mime=mime):
                    reply = response(body, content_type=mime)
                    self.assertEqual(deletion_outcome(reply, 647172).code, OutcomeCode.UNKNOWN)
                    self.assertEqual(response_diagnostics(reply)['decision_reason'], reason)

    def test_non_success_http_status_overrides_even_a_success_body(self):
        for status, code, reason in ((401, OutcomeCode.AUTH_REQUIRED, 'http_auth_required'),
                                    (403, OutcomeCode.FORBIDDEN, 'http_forbidden'),
                                    (429, OutcomeCode.RATE_LIMITED, 'http_rate_limited'),
                                    (302, OutcomeCode.UNKNOWN, 'http_status_unverified'),
                                    (500, OutcomeCode.UNKNOWN, 'http_status_unverified')):
            with self.subTest(status=status):
                reply = response('{"result":"success"}', status=status)
                self.assertEqual(deletion_outcome(reply, 647172).code, code)
                self.assertEqual(response_diagnostics(reply)['decision_reason'], reason)

    def test_response_shape_never_exposes_raw_values_or_unknown_keys(self):
        observed = response_diagnostics(response('{"result":"success","msg":"synthetic-secret-message","secret-key":"value"}'))
        self.assertEqual(observed['content_type_kind'], 'html')
        self.assertEqual(observed['result_kind'], 'success')
        self.assertEqual(observed['unknown_field_count'], 1)
        self.assertNotIn('secret', json.dumps(observed))
        self.assertFalse(response_diagnostics(response('synthetic-secret'))['valid_json'])
