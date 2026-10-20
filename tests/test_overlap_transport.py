from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from io import BytesIO
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch
from urllib.request import HTTPSHandler, HTTPCookieProcessor, ProxyHandler, build_opener

from rectifier.auth import MemorySession, _NoRedirect


class OverlapTransportTests(TestCase):
    def test_existing_memory_transport_overlaps_get_and_post_without_exporting_cookies(self):
        barrier = Barrier(2)
        class FakeResponse(BytesIO):
            status = code = 200
            msg = 'OK'
            def __init__(self, request):
                super().__init__(b'ok')
                self.headers = Message()
                self.headers['Set-Cookie'] = request.method.lower() + '=synthetic; Path=/; Secure'
                self.url = request.full_url
            def info(self):
                return self.headers
            def geturl(self):
                return self.url
        class Handler(HTTPSHandler):
            def https_open(self, request):
                barrier.wait(timeout=3)
                return FakeResponse(request)
        session = MemorySession()
        session._opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(session._cookies),
                                      _NoRedirect(), Handler())
        try:
            with patch('socket.socket', side_effect=AssertionError('offline only')):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    get = pool.submit(session.request, 'GET', 'https://gall.dcinside.com/')
                    post = pool.submit(session.request, 'POST', 'https://gall.dcinside.com/', form=[('id', 'mnt')])
                    self.assertEqual(get.result().status, 200)
                    self.assertEqual(post.result().status, 200)
            self.assertEqual(session.cookie_value('get', 'https://gall.dcinside.com/'), 'synthetic')
            self.assertEqual(session.cookie_value('post', 'https://gall.dcinside.com/'), 'synthetic')
        finally:
            session.close()
        self.assertFalse(list(session._cookies))
