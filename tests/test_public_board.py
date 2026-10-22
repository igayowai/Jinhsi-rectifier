from email.message import Message
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from rectifier.public_board import (MAX_HTML_BYTES, NoRedirect, PublicReadError, PublicServerError,
                                    read_public_board)


class Response(BytesIO):
    def __init__(self, data=b"<table></table>", content_type="text/html; charset=utf-8", status=200):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.status = status


class PublicBoardTests(TestCase):
    def test_single_anonymous_get_and_fixed_host(self):
        with patch("rectifier.public_board.build_opener") as builder:
            builder.return_value.open.return_value = Response()
            self.assertEqual(read_public_board("mnt", 8000), "<table></table>")
            builder.return_value.open.assert_called_once()
            request = builder.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://gall.dcinside.com/mgallery/board/lists/?id=mnt&page=8000")
            self.assertNotIn("Cookie", request.headers)
            self.assertEqual(request.get_method(), "GET")

    def test_bad_inputs_do_not_open_network(self):
        with patch("rectifier.public_board.build_opener") as builder:
            for gallery, page in [("https://other.test", 1), ("mnt", 0), ("mnt", True)]:
                with self.subTest(gallery=gallery, page=page), self.assertRaises(ValueError):
                    read_public_board(gallery, page)
            builder.assert_not_called()

    def test_rejections_are_not_retried(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status), patch("rectifier.public_board.build_opener") as builder:
                builder.return_value.open.side_effect = HTTPError("https://gall.dcinside.com", status, "raw", {}, None)
                with self.assertRaises(PublicReadError):
                    read_public_board("mnt", 1)
                builder.return_value.open.assert_called_once()

    def test_only_5xx_is_reported_as_server_outage(self):
        for status, outage in ((429, False), (403, False), (500, True), (504, True)):
            with self.subTest(status=status), patch("rectifier.public_board.build_opener") as builder:
                builder.return_value.open.side_effect = HTTPError("https://gall.dcinside.com", status, "raw", {}, None)
                with self.assertRaises(PublicReadError) as caught:
                    read_public_board("mnt", 1)
                self.assertIs(isinstance(caught.exception, PublicServerError), outage)

    def test_redirects_never_follow(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_bad_content_and_oversize_are_rejected(self):
        for response in [Response(content_type="application/json"), Response(status=204),
                         Response(b"x" * (MAX_HTML_BYTES + 1)), Response(b"\xff")]:
            with self.subTest(response=response), patch("rectifier.public_board.build_opener") as builder:
                builder.return_value.open.return_value = response
                with self.assertRaises(PublicReadError):
                    read_public_board("mnt", 1)
