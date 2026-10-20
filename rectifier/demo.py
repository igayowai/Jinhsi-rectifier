"""Synthetic, network-free list HTML for the offline demo command and tests."""


DEMO_HTML = """<table class="gall_list"><tbody>
<tr class="ub-content"><td class="gall_num">1</td><td class="gall_subject">일반</td>
<td class="gall_tit"><a href="/mgallery/board/view/?id=mnt&amp;no=1">가상 비회원 글</a></td>
<td class="gall_writer" data-uid="" data-ip="192.0.2.1">예시(192.0)</td></tr>
<tr class="ub-content"><td class="gall_num">2</td><td class="gall_subject">일반</td>
<td class="gall_tit"><a href="/mgallery/board/view/?id=mnt&amp;no=2">가상 반고닉 글</a></td>
<td class="gall_writer" data-uid="synthetic_member" data-ip="">회원</td></tr>
<tr class="ub-content"><td class="gall_num">3</td><td class="gall_subject">일반</td>
<td class="gall_tit"><a href="/mgallery/board/view/?id=mnt&amp;no=3">가상 판정 불가 글</a></td>
<td class="gall_writer">정보 누락</td></tr></tbody></table>"""
