"""Read-only management UI diagnostics. These signals never authorize deletion."""
from html.parser import HTMLParser

from .models import validate_gallery
from .parser import parse_board

from .diagnostics import record_error


DESKTOP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
}


class _Controls(HTMLParser):
    def __init__(self, html, *, allow_template=True, onclicks=('del_list()',)):
        super().__init__(convert_charrefs=True)
        self.divs = []
        self.inert = []
        self.tables = 0
        self.buttons = []
        self.templates = []
        self.template_text = None
        self.allow_template = allow_template
        self.onclicks = onclicks
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        duplicate = len(a) != len(attrs)
        if (self.allow_template and not self.inert and not self.tables and tag == "script"
                and a.get("id") == "minor_buttons-tmpl"):
            self.templates.append(False)
            if not duplicate and a.get("type") == "text/x-jquery-tmpl" and "src" not in a:
                self.template_text = []
        if tag in {"script", "style", "template", "noscript"}:
            self.inert.append(tag)
        if self.inert:
            return
        if tag == "table":
            self.tables += 1
        if tag == "div":
            self.divs.append(set() if duplicate else set((a.get("class") or "").split()))
        if (tag == "button" and not self.tables and len(self.divs) >= 2
                and "useradmin_btnbox" in self.divs[-1]
                and "list_bottom_btnbox" in self.divs[-2]
                and (a.get("onclick") or "").strip() in self.onclicks):
            self.buttons.append(not duplicate and "disabled" not in a
                                and a.get("aria-disabled", "false") == "false")

    def handle_endtag(self, tag):
        if self.inert:
            if tag == self.inert[-1]:
                self.inert.pop()
                if tag == "script" and self.template_text is not None:
                    html = "".join(self.template_text)
                    self.template_text = None
                    # Official code appends this static template to this div.
                    # Never execute JavaScript or interpret conditional templates.
                    if "{{" not in html and "${" not in html:
                        parsed = _Controls('<div class="list_bottom_btnbox">' + html + '</div>',
                                           allow_template=False, onclicks=self.onclicks)
                        self.templates[-1] = parsed.buttons == [True]
            return
        if tag == "div" and self.divs:
            self.divs.pop()
        if tag == "table" and self.tables:
            self.tables -= 1

    def handle_data(self, data):
        if self.template_text is not None:
            self.template_text.append(data)


def check_management(login_result, gallery_id):
    """One GET after verified login; no redirects, retries, raw HTML or tokens.

    An enabled control is UI evidence only. A server can still reject a later
    action because of changed permissions or limits.
    """
    validate_gallery(gallery_id)
    output = {"gallery_id": gallery_id, "status": "not_checked",
              "http_status": None, "delete_control_observed": False,
              "delete_control_source": None,
              "server_delete_verified": False}
    if not login_result.identity_verified or login_result.session is None:
        output["status"] = "identity_unverified"
        return output
    try:
        response = login_result.session.request(
            "GET", f"https://gall.dcinside.com/mgallery/board/lists/?id={gallery_id}",
            headers=DESKTOP_HEADERS)
        output["http_status"] = response.status
        if response.status != 200:
            output["status"] = "http_error"
            return output
        if not response.body.strip():
            output['status'] = 'empty_response'
            return output
        # Reuse the existing conservative gallery/post parser before UI checks.
        posts = parse_board(response.body, gallery_id)
        if not posts:
            output["status"] = "page_unverified"
            return output
        controls = _Controls(response.body)
        direct = controls.buttons == [True] and not controls.templates
        template = controls.templates == [True] and not controls.buttons
        output["delete_control_observed"] = direct or template
        output["delete_control_source"] = "html_button" if direct else "server_template" if template else None
        output["status"] = "control_observed" if direct or template else "control_unverified"
    except ValueError:
        output["status"] = "page_unverified"
    except Exception:
        record_error('management.check_management')
        output["status"] = "inspection_failed"
    return output
