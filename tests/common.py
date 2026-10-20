"""Shared synthetic fixtures for offline tests. No real accounts or network."""
from rectifier.auth import HttpResponse
from tests.test_parser import board, post_row

ACCOUNT = 'syntheticadmin'
VIEW = '''<input id="gallery_id" value="mnt"><input id="no" value="647172">
<input id="_GALLTYPE_" value="M">
<div class="gallview_head">
<span class="title_subject">synthetic old post</span>
<div class="gall_writer" data-uid="" data-ip="192.0.2.1"></div>
<span class="gall_date" title="2023-11-23 18:42:52">2023.11.23 18:42:52</span>
<span class="gall_reply_num">추천 7</span><span class="gall_comment">댓글 0</span></div>
<div class="writing_view_box"><div class="write_div">synthetic text</div></div>'''
MENU = '''<header class="header"><ul id="top_depthbox">
<li><a href="https://m.dcinside.com/gallog/syntheticadmin">me</a></li>
<li><a href="https://msign.dcinside.com/logout">out</a></li></ul></header>'''
CONTROL = '<div class="list_bottom_btnbox"><div class="useradmin_btnbox"><button onclick="del_list()">삭제</button></div></div>'


def response(body, status=200, content_type='text/html'):
    return HttpResponse(status, {'Content-Type': content_type}, body)


IDS = (101, 102)


def listing(ids=IDS, *, kind='icon_txt', writer='data-uid="" data-ip="192.0"'):
    return board(*(post_row(number=str(pid), attrs=f'data-type="{kind}" data-no="{pid}"',
        title='synthetic old post', writer=writer,
        link=f'/mgallery/board/view/?id=mnt&amp;no={pid}') for pid in ids))
