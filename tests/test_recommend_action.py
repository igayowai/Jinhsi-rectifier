from dataclasses import replace
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.management import _Controls
from rectifier.page_preview import preview_pages
from rectifier.recommend_action import (RecommendReleaseAction, RELEASE_URL, RELEASE_CONTROLS,
                                       format_release_result, validate_release_review)
from rectifier.selection import ManualSelection
from tests.common import listing
from tests.common import MENU, CONTROL, response


RELEASE_CONTROL = CONTROL.replace('del_list()', "update_recom('REL')")


def reviewed(pages=(1,), ids=(101, 102), writer='data-uid="" data-ip="192.0"'):
    model = ManualSelection()
    generation = model.begin('mnt', pages)
    result = preview_pages('mnt', 'synthetic', pages,
        reader=lambda *_: listing(ids, kind='icon_recomimg', writer=writer))
    model.apply(generation, result)
    model.select('all')
    return model.review()


class RecommendActionTests(TestCase):
    def setUp(self):
        self.no_network = patch('socket.socket', side_effect=AssertionError('offline only'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.cancel = Event()
        self.session = Mock()
        self.session.cookie_value.return_value = 'synthetic-token'
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, 'syntheticadmin')

    def arrange(self, *, review=None, before=None, after=None, post=None, control=RELEASE_CONTROL,
                menu=MENU, on_post=None, clock=None):
        review = reviewed() if review is None else review
        ids = tuple(row.post_id for row in review.rows)
        before = listing(ids, kind='icon_recomimg') if before is None else before
        after = listing(ids) if after is None else after
        posted = False

        def request(method, url, **kwargs):
            nonlocal posted
            if method == 'POST':
                self.assertEqual(url, RELEASE_URL)
                self.assertFalse(posted)
                posted = True
                if on_post:
                    on_post()
                if isinstance(post, Exception):
                    raise post
                return response('{"result":"success"}') if post is None else post
            if url == DEFAULT_RETURN_URL:
                return response(menu)
            self.assertIn('/mgallery/board/lists/?id=mnt&page=', url)
            if posted and isinstance(after, Exception):
                raise after
            body = after if posted else before
            if isinstance(body, dict):
                body = body[int(url.rsplit('=', 1)[1])]
            return response(body + control)

        self.session.request.side_effect = request
        return RecommendReleaseAction(self.login, review, **({'clock': clock} if clock else {}))

    def posts(self):
        return [c for c in self.session.request.call_args_list if c.args[0] == 'POST']

    def test_one_bulk_post_exact_ids_rel_and_read_back(self):
        action = self.arrange()
        self.session.request.assert_not_called()
        receipt = action.execute(self.cancel)
        self.assertEqual(receipt.released, (101, 102))
        self.assertEqual(receipt.unknown, ())
        form = self.posts()[0].kwargs['form']
        self.assertEqual([v for k, v in form if k == 'nos[]'], ['101', '102'])
        self.assertEqual(dict(form)['mode'], 'REL')
        self.assertEqual(dict(form)['_GALLTYPE_'], 'M')
        self.assertEqual(len(self.posts()), 1)
        second = action.execute(self.cancel)
        self.assertFalse(second.attempted)
        self.assertEqual(second.stopped, 'already_used')
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn('synthetic-token', repr(receipt))

    def test_members_supported_without_changing_delete_policy(self):
        writer = 'data-uid="synthetic-member"'
        action = self.arrange(review=reviewed(writer=writer),
            before=listing(kind='icon_recomimg', writer=writer), after=listing(writer=writer))
        self.assertEqual(action.execute(self.cancel).released, (101, 102))

    def test_text_recommendation_is_recognized_and_not_delete_candidate(self):
        from rectifier.parser import parse_board_entries
        from rectifier.page_preview import classify_list_entries
        html = listing(kind='icon_recomtxt')
        classified = classify_list_entries('mnt', parse_board_entries(html, 'mnt'))
        self.assertEqual(classified['batch_candidate_count'], 0)
        self.assertTrue(all(item['post_kind'] == 'recommended' for item in classified['items']))
        self.assertEqual(self.arrange(before=html).execute(self.cancel).released, (101, 102))

    def test_250_selected_once_and_no_unselected_number(self):
        ids = tuple(range(1, 251))
        action = self.arrange(review=reviewed(ids=ids),
                             before=listing((*ids, 999), kind='icon_recomimg'))
        self.assertEqual(action.execute(self.cancel).released, ids)
        sent = [v for k, v in self.posts()[0].kwargs['form'] if k == 'nos[]']
        self.assertEqual(sent, list(map(str, ids)))

    def test_partial_release_absence_is_unknown_not_success(self):
        action = self.arrange(review=reviewed(pages=(1, 2, 3), ids=(101, 102, 103)),
            after={1: listing((101,)), 2: listing((102,), kind='icon_recomimg'), 3: listing(())})
        result = action.execute(self.cancel)
        self.assertEqual((result.released, result.remaining, result.unknown), ((101,), (102,), (103,)))
        self.assertIn('미확인 1개', format_release_result(result))

    def test_response_is_not_the_state_confirmation(self):
        for post in (response(''), response('<html>login</html>'), response('{}'),
                     response('{"result":"success"}'), response('error', 500), TimeoutError('synthetic')):
            with self.subTest(post=type(post).__name__):
                self.session.reset_mock()
                result = self.arrange(post=post, after=listing(kind='icon_recomimg')).execute(self.cancel)
                self.assertEqual(result.released, ())
                self.assertEqual(result.remaining, (101, 102))
                self.assertEqual(len(self.posts()), 1)

    def test_timeout_followed_by_observed_release_no_retry(self):
        result = self.arrange(post=TimeoutError('synthetic')).execute(self.cancel)
        self.assertEqual(result.released, (101, 102))
        self.assertEqual(len(self.posts()), 1)

    def test_read_back_failure_preserves_uncertainty(self):
        result = self.arrange(after=OSError('synthetic')).execute(self.cancel)
        self.assertTrue(result.attempted)
        self.assertEqual(result.unknown, (101, 102))
        self.assertEqual(result.stopped, 'read_failed')

    def test_401_403_429_stop_without_more_reads(self):
        for code, reason in ((401, 'auth_required'), (403, 'forbidden'), (429, 'rate_limited')):
            with self.subTest(code=code):
                self.session.reset_mock()
                result = self.arrange(post=response('', code)).execute(self.cancel)
                self.assertEqual(result.stopped, reason)
                self.assertEqual(result.unknown, (101, 102))
                self.assertEqual(self.session.request.call_count, 3)

    def test_control_missing_disabled_duplicate_or_delete_only_stops(self):
        for control in ('', CONTROL, RELEASE_CONTROL * 2,
                        RELEASE_CONTROL.replace('<button ', '<button disabled '),
                        '<script>' + RELEASE_CONTROL + '</script>'):
            with self.subTest(control=control[:20]):
                self.session.reset_mock()
                result = self.arrange(control=control).execute(self.cancel)
                self.assertFalse(result.attempted)
                self.assertEqual(result.stopped, 'release_control_unverified')
                self.assertEqual(self.posts(), [])

    def test_static_template_supported_but_delete_permission_not_release(self):
        control = ('<script id="minor_buttons-tmpl" type="text/x-jquery-tmpl">'
                   '<div class="useradmin_btnbox"><button onclick="update_recom(\'REL\');">'
                   '해제</button></div></script>')
        self.assertEqual(_Controls(control).templates, [False])
        self.assertEqual(_Controls(control, onclicks=RELEASE_CONTROLS).templates, [True])
        self.assertEqual(self.arrange(control=control).execute(self.cancel).released, (101, 102))

    def test_selection_title_kind_gallery_or_account_changes_stop_before_post(self):
        cases = [dict(before=listing()), dict(before=listing((101,), kind='icon_recomimg')),
                 dict(before=listing(kind='icon_recomimg').replace('synthetic old post', 'changed')),
                 dict(before=listing(kind='icon_recomimg').replace('id=mnt', 'id=fixture')),
                 dict(menu=MENU.replace('syntheticadmin', 'different'))]
        for case in cases:
            with self.subTest(case=list(case)):
                self.session.reset_mock()
                self.assertFalse(self.arrange(**case).execute(self.cancel).attempted)
                self.assertEqual(self.posts(), [])

    def test_cross_page_notice_and_conflicting_kind_are_excluded(self):
        for kind in ('icon_notice', 'icon_txt'):
            with self.subTest(kind=kind):
                self.session.reset_mock()
                result = self.arrange(review=reviewed(pages=(1, 2)), before={
                    1: listing(kind='icon_recomimg'), 2: listing(kind=kind)}).execute(self.cancel)
                self.assertEqual(result.stopped, 'selection_changed')
                self.assertFalse(result.attempted)

    def test_cancel_before_and_after_post(self):
        self.cancel.set()
        self.assertFalse(self.arrange().execute(self.cancel).attempted)
        self.assertEqual(self.posts(), [])
        self.cancel.clear()
        result = self.arrange(on_post=self.cancel.set).execute(self.cancel)
        self.assertTrue(result.attempted)
        self.assertEqual(result.stopped, 'user_stopped')
        self.assertEqual(result.unknown, (101, 102))
        self.assertEqual(len(self.posts()), 1)

    def test_cookie_missing_stop_during_cookie_and_expiry_prevent_post(self):
        self.session.cookie_value.return_value = ''
        self.assertFalse(self.arrange().execute(self.cancel).attempted)
        self.session.cookie_value.side_effect = lambda *_: (self.cancel.set() or 'synthetic-token')
        self.assertFalse(self.arrange().execute(self.cancel).attempted)
        self.assertEqual(self.posts(), [])
        self.cancel.clear()
        self.session.cookie_value.side_effect = None
        clock = Mock(side_effect=[0, 61])
        self.assertEqual(self.arrange(clock=clock).execute(self.cancel).stopped, 'stale_listing')

    def test_invalid_review_is_inert(self):
        good = reviewed()
        for review in (replace(good, complete=False), replace(good, rows=()),
                       replace(good, rows=good.rows * 2), reviewed(ids=tuple(range(1, 252))),
                       replace(good, rows=(replace(good.rows[0], post_kind='ordinary'),)),
                       replace(good, rows=(replace(good.rows[0], disposition='unknown_author'),))):
            with self.subTest(count=len(review.rows)):
                with self.assertRaises(ValueError):
                    validate_release_review(review)
        self.session.request.assert_not_called()
