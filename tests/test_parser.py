from pathlib import Path
import unittest

from rectifier.models import AuthorKind
from rectifier.parser import BoardParseError, parse_board


FIXTURE = Path(__file__).parent / "fixtures" / "mixed_board.html"
LINK = "/mgallery/board/view/?id=fixture&amp;no=101"


def post_row(writer='data-uid="" data-ip="192.0"', *, link=LINK, number="101",
             title="synthetic title", extra="", attrs=""):
    return (f'<tr {attrs}><td class="gall_num">{number}</td>'
            f'<td class="gall_tit"><a href="{link}">{title}</a>{extra}</td>'
            f'<td class="gall_writer" {writer}>synthetic nickname</td></tr>')


def board(*rows):
    return '<table class="gall_list"><tbody>' + ''.join(rows) + '</tbody></table>'


class ParserTests(unittest.TestCase):
    def test_mixed_fixture_keeps_members_and_unknowns_distinct(self):
        posts = parse_board(FIXTURE.read_text(encoding="utf-8"), "fixture")
        self.assertEqual([post.post_id for post in posts], [106, 105, 104, 103, 102, 101])
        self.assertEqual([post.author_kind for post in posts], [
            AuthorKind.GUEST, AuthorKind.MEMBER, AuthorKind.MEMBER,
            AuthorKind.UNKNOWN, AuthorKind.UNKNOWN, AuthorKind.GUEST])
        self.assertEqual(posts[1].uid, "synthetic_fixed")
        self.assertEqual(posts[2].uid, "synthetic_semi")
        self.assertNotIn("192.0", repr(posts))
        self.assertNotIn("합성 이름", repr(posts))

    def test_missing_empty_or_valueless_uid_are_different(self):
        for writer, kind in [
            ('data-ip="192.0"', AuthorKind.UNKNOWN),
            ('data-uid data-ip="192.0"', AuthorKind.UNKNOWN),
            ('data-uid="" data-ip="192.0"', AuthorKind.GUEST),
            ('data-uid="synthetic_user"', AuthorKind.MEMBER),
        ]:
            with self.subTest(writer=writer):
                self.assertEqual(parse_board(board(post_row(writer)), "fixture")[0].author_kind, kind)

    def test_ip_display_accepts_only_valid_partial_or_full_ipv4(self):
        valid = ("0.0", "192.0", "255.255", "192.0.2.3", "203.0.113.255")
        invalid = ("", "256.0", "192.999", "192.0.2", "192.0.2.256", "01.2", "192.0.*.*",
                   " 192.0", "192.0 ", "192.0x", "192.0/16", "1", "::1", "1.2\n", "１.２")
        for value in valid + invalid:
            with self.subTest(value=value):
                post = parse_board(board(post_row(f'data-uid="" data-ip="{value}"')), "fixture")[0]
                self.assertEqual(post.author_kind, AuthorKind.GUEST if value in valid else AuthorKind.UNKNOWN)

    def test_conflicting_or_malformed_member_fields_are_unknown(self):
        for writer in ('data-uid="member" data-ip="192.0"', 'data-uid=" member"',
                       'data-uid="member "', 'data-uid="<bad>"', 'data-uid="a/b"',
                       'data-uid="" data-uid="member" data-ip="192.0"',
                       'data-uid="" data-ip="192.0" data-ip="198.51"'):
            with self.subTest(writer=writer):
                self.assertEqual(parse_board(board(post_row(writer)), "fixture")[0].author_kind, AuthorKind.UNKNOWN)

    def test_absent_or_duplicate_writer_is_unknown(self):
        row = post_row()
        missing = row.replace('<td class="gall_writer" data-uid="" data-ip="192.0">synthetic nickname</td>', '')
        duplicate = row.replace('</tr>', '<td class="gall_writer" data-uid="member"></td></tr>')
        for value in (missing, duplicate):
            with self.subTest(value=value):
                self.assertEqual(parse_board(board(value), "fixture")[0].author_kind, AuthorKind.UNKNOWN)

    def test_https_and_root_relative_links_accept_same_target(self):
        for link in (LINK, "https://gall.dcinside.com" + LINK,
                     "/mgallery/board/view?id=fixture&amp;no=101&amp;page=2"):
            with self.subTest(link=link):
                self.assertEqual(parse_board(board(post_row(link=link)), "fixture")[0].post_id, 101)

    def test_unsafe_or_mismatched_links_reject_page(self):
        links = (
            "http://gall.dcinside.com" + LINK,
            "https://gall.dcinside.com.evil.example" + LINK,
            "https://gall.dcinside.com@evil.example" + LINK,
            "https://user@gall.dcinside.com" + LINK,
            "https://gall.dcinside.com:443" + LINK,
            "//gall.dcinside.com" + LINK,
            LINK.replace("/mgallery/", "/"), LINK.replace("/view/", "/lists/"),
            LINK.replace("fixture", "other_gallery"), LINK.replace("101", "102"),
            LINK + "&amp;id=other_gallery", LINK + "&amp;no=102",
            LINK.replace("no=101", "no=00101"), LINK.replace("no=101", "no=-1"),
            LINK.replace("&amp;no=101", ""), LINK + "#unexpected",
            "javascript:alert(1)", " " + LINK, LINK + "&#10;", LINK + "&amp;broken",
        )
        for link in links:
            with self.subTest(link=link):
                with self.assertRaises(BoardParseError):
                    parse_board(board(post_row(link=link)), "fixture")

    def test_foreign_gallery_row_does_not_yield_partial_page(self):
        with self.assertRaises(BoardParseError):
            parse_board(board(post_row(), post_row(link=LINK.replace("fixture", "other"))), "fixture")

    def test_duplicate_title_cells_links_and_attributes_reject_page(self):
        mutations = (
            post_row(extra=f'<a href="{LINK}">second title</a>'),
            post_row().replace('</tr>', f'<td class="gall_tit"><a href="{LINK}">second</a></td></tr>'),
            post_row().replace(f'href="{LINK}"', f'href="{LINK}" href="{LINK}"'),
            post_row().replace('class="gall_num"', 'class="gall_num" class="other"'),
            post_row().replace('</tr>', '<td class="gall_num">101</td></tr>'),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(BoardParseError):
                    parse_board(board(mutation), "fixture")

    def test_reply_link_must_be_unique_and_match_article(self):
        reply = f'<a class="reply_numbox" href="{LINK}#comment_view">[2]</a>'
        self.assertEqual(parse_board(board(post_row(extra=reply)), "fixture")[0].title, "synthetic title")
        for extra in (reply + reply, reply.replace("no=101", "no=102"), reply.replace("fixture", "other")):
            with self.subTest(extra=extra):
                with self.assertRaises(BoardParseError):
                    parse_board(board(post_row(extra=extra)), "fixture")

    def test_special_rows_are_excluded_even_without_post_links(self):
        rows = [f'<tr><td class="gall_num">{label}</td><td class="gall_tit">synthetic special</td></tr>'
                for label in ("공지", "광고", "AD", "설문", "투표")]
        rows += [post_row(attrs='data-type="icon_notice"'), post_row(attrs='data-type="icon_vote"')]
        self.assertEqual(parse_board(board(*rows), "fixture"), ())

    def test_dash_number_survey_and_ad_rows_are_excluded(self):
        survey = ('<tr class="ub-content"><td class="gall_num">-</td>'
                  '<td class="gall_subject">설문</td><td class="gall_tit ub-word">'
                  '<a class="font_jinred" href=";">synthetic survey</a></td>'
                  '<td class="gall_writer ub-writer">synthetic service label</td></tr>')
        advert = ('<tr class="ub-content"><td class="gall_num">-</td>'
                  '<td class="gall_subject">AD</td><td class="gall_tit ub-word">'
                  '<a href="https://invalid.example/synthetic-ad">synthetic ad</a></td>'
                  '<td class="gall_writer ub-writer">synthetic service label</td></tr>')
        posts = parse_board(board(survey, advert, post_row()), "fixture")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].post_id, 101)
        self.assertEqual(posts[0].author_kind, AuthorKind.GUEST)

    def test_dash_number_does_not_hide_unknown_or_normal_post_rows(self):
        special = ('<tr class="ub-content"><td class="gall_num">-</td>'
                   '<td class="gall_subject">AD</td><td class="gall_tit">synthetic ad</td></tr>')
        invalid = (
            special.replace('ub-content', 'ub-content us-post'),
            special.replace('<tr class=', '<tr data-no="101" class='),
            special.replace('>AD<', '>unknown subject<'),
            special.replace('<td class="gall_subject">AD</td>', ''),
            special.replace('<td class="gall_subject">AD</td>', '<td class="gall_subject">AD</td>' * 2),
            special.replace('class="gall_subject"', 'class="gall_subject" class="changed"'),
            post_row(number="-"),
        )
        for row in invalid:
            with self.subTest(row=row):
                with self.assertRaises(BoardParseError):
                    parse_board(board(row), "fixture")

    def test_invalid_numbers_and_conflicting_data_no_reject(self):
        for number in ("0", "-1", "00101", "1e2", "１０１", "변경됨", "9" * 21):
            with self.subTest(number=number):
                with self.assertRaises(BoardParseError):
                    parse_board(board(post_row(number=number)), "fixture")
        with self.assertRaises(BoardParseError):
            parse_board(board(post_row(attrs='data-no="102"')), "fixture")

    def test_duplicate_ordinary_post_ids_reject(self):
        with self.assertRaises(BoardParseError):
            parse_board(board(post_row(), post_row()), "fixture")

    def test_all_new_rows_are_kept_without_an_eight_post_limit(self):
        rows = [post_row(number=str(number), link=LINK.replace("101", str(number)))
                for number in range(112, 100, -1)]
        self.assertEqual([post.post_id for post in parse_board(board(*rows), "fixture")],
                         list(range(112, 100, -1)))

    def test_overlapping_field_classes_and_nested_sections_reject(self):
        overlap = post_row().replace('class="gall_tit"', 'class="gall_tit gall_writer"')
        nested = post_row().replace('synthetic nickname', '<tbody>' + post_row() + '</tbody>')
        for value in (overlap, nested):
            with self.subTest(value=value):
                with self.assertRaises(BoardParseError):
                    parse_board(board(value), "fixture")

    def test_structural_mismatch_rejects_instead_of_reporting_empty_list(self):
        good = board(post_row())
        for html in ("", "<html>login</html>", good.replace("gall_list", "changed_list"),
                     good + good, good.replace('</td>', '', 1), good.replace('</tr>', ''),
                     good.replace('</table>', ''), good.replace('<td class="gall_num">101</td>', ''),
                     good.replace('<tbody>', '<tbody><table>')):
            with self.subTest(html=html):
                with self.assertRaises(BoardParseError):
                    parse_board(html, "fixture")

    def test_empty_table_and_column_headers_are_valid(self):
        self.assertEqual(parse_board(board(), "fixture"), ())
        self.assertEqual(parse_board(board('<tr><th>번호</th><th>제목</th></tr>'), "fixture"), ())

    def test_title_entities_are_decoded_without_author_display(self):
        post = parse_board(board(post_row(title='A &amp; B <span>&lt;fixture&gt;</span>')), "fixture")[0]
        self.assertEqual(post.title, "A & B <fixture>")
        self.assertNotIn("synthetic nickname", repr(post))

    def test_inert_tables_cannot_be_treated_as_board(self):
        for tag in ("script", "template", "noscript"):
            with self.subTest(tag=tag):
                with self.assertRaises(BoardParseError):
                    parse_board(f'<{tag}>' + board(post_row()) + f'</{tag}>', "fixture")

    def test_empty_title_and_inert_title_children_reject(self):
        for title in ("", "   ", 'title<script>synthetic</script>', 'title<template>synthetic</template>'):
            with self.subTest(title=title):
                with self.assertRaises(BoardParseError):
                    parse_board(board(post_row(title=title)), "fixture")

    def test_invalid_input_gallery_rejects(self):
        for gallery in ("", "../fixture", "other&no=1", None):
            with self.subTest(gallery=gallery):
                with self.assertRaises(ValueError):
                    parse_board(board(post_row()), gallery)
        with self.assertRaises(BoardParseError):
            parse_board(None, "fixture")


if __name__ == "__main__":
    unittest.main()
