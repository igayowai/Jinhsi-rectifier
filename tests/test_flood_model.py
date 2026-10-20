from dataclasses import replace
from unittest import TestCase

from rectifier.models import AuthorKind
from rectifier.policy import preview_page_items
from tests.flood_model import Scenario, latency_summary, parsed_page, simulate


class FloodModelTests(TestCase):
    def test_exact_small_timeline(self):
        result = simulate(Scenario(rate=10, duration_ms=200, grace_ms=100,
                                   cycle_ms=100, latency_ms=10, delete_rate=0))
        self.assertEqual(result['generated'], 2)
        self.assertEqual(result['discovered'], 2)
        self.assertEqual(result['discovery_latency'],
                         {'p50_ms': 10, 'p95_ms': 10, 'max_ms': 10})
        self.assertEqual(result['mock_deleted'], 0)

    def test_single_page_loses_posts_above_capacity(self):
        s = Scenario(rate=100, duration_ms=3000)
        one, three = simulate(s), simulate(replace(s, pages=3))
        self.assertEqual(one['missed'], 150)
        self.assertEqual(three['missed'], 0)
        self.assertEqual(one['generated'], 300)

    def test_delay_can_cause_loss_even_at_40_per_second(self):
        s = Scenario(duration_ms=5000, slow_requests=((2, 2500),))
        self.assertGreater(simulate(s)['missed'], 0)
        self.assertEqual(simulate(replace(s, pages=3))['missed'], 0)

    def test_read_failure_is_not_success_or_empty_board(self):
        r = simulate(Scenario(duration_ms=3000, failed_requests=(2, 3)))
        self.assertEqual(r['failed_responses'], 2)
        self.assertFalse(r['all_responses_succeeded'])
        self.assertGreater(r['missed'], 0)

    def test_backlog_when_deletion_is_slower_than_arrival(self):
        r = simulate(Scenario(pages=3, cycle_ms=250, delete_rate=20))
        self.assertGreater(r['at_load_end']['remaining'], 200)
        self.assertLessEqual(r['at_load_end']['mock_deleted'], 200)
        self.assertEqual(r['queued'], r['discovered'] - r['mock_deleted'])

    def test_no_banked_deletion_capacity_before_discovery(self):
        r = simulate(Scenario(rate=10, duration_ms=100, grace_ms=1,
                              cycle_ms=100, latency_ms=1, delete_rate=100))
        self.assertEqual(r['discovered'], 1)
        self.assertEqual(r['mock_deleted'], 0)
        self.assertEqual(r['queued'], 1)

    def test_new_posts_shift_pages_duplicates_are_not_new_candidates(self):
        first = parsed_page(range(100, 50, -1))
        second = parsed_page(range(70, 20, -1))  # 20 insertions before page 2.
        items = preview_page_items('fixture', first + second)
        self.assertEqual(len(items), 80)
        self.assertEqual(len({item.post_id for item in items}), 80)

    def test_deletion_between_reads_skips_ids_without_any_network_failure(self):
        # Page 1 returns100..51; deletion of those50 shifts page2 to empty.
        first = parsed_page(range(100, 50, -1))
        remaining = list(range(50, 0, -1))
        second = parsed_page(remaining[50:100])
        items = preview_page_items('fixture', first + second)
        self.assertEqual(set(range(1, 101)) - {i.post_id for i in items}, set(range(1, 51)))

    def test_conflict_and_member_protection_survive_page_merge(self):
        guest = parsed_page([1])[0]
        member = replace(guest, author_kind=AuthorKind.MEMBER, uid='synthetic_member')
        self.assertEqual(preview_page_items('fixture', [guest, member])[0].disposition,
                         'conflicting_metadata')
        self.assertEqual(preview_page_items('fixture', [member, member])[0].disposition,
                         'protected_member')

    def test_deterministic_and_count_conservation(self):
        s = Scenario(rate=30, duration_ms=1000, pages=3, delete_rate=60)
        r = simulate(s)
        self.assertEqual(r, simulate(s))
        self.assertEqual(r['generated'], r['discovered'] + r['missed'])
        self.assertEqual(r['discovered'], r['mock_deleted'] + r['queued'])

    def test_percentiles_use_nearest_rank_and_empty_is_unknown(self):
        self.assertEqual(latency_summary(range(1, 101)),
                         {'p50_ms': 50, 'p95_ms': 95, 'max_ms': 100})
        self.assertIsNone(latency_summary([])['p95_ms'])

    def test_invalid_model_parameters(self):
        for kwargs in ({'rate': 0}, {'rate': True}, {'latency_ms': 0}, {'pages': 11},
                       {'delete_rate': -1}, {'failed_requests': (0,)},
                       {'slow_requests': ((1, 0),)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Scenario(**kwargs)
