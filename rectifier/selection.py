"""Ephemeral manual selection; never a deletion plan or row-position selection."""
from dataclasses import dataclass
import re

from .models import validate_gallery, validate_post_id
from .page_preview import MAX_PREVIEW_PAGES


def parse_pages(text):
    parts = re.split(r"[\s,]+", text.strip())
    if (not 1 <= len(parts) <= MAX_PREVIEW_PAGES
            or any(not re.fullmatch(r"[0-9]{1,7}", part) for part in parts)):
        raise ValueError("페이지는 쉼표나 공백으로 구분한 숫자 1~5개로 입력")
    pages = tuple(map(int, parts))
    if any(not 1 <= page <= 1_000_000 for page in pages) or len(set(pages)) != len(pages):
        raise ValueError("페이지는 중복 없이 1~1000000 범위로 입력")
    return pages


@dataclass(frozen=True)
class SelectionRow:
    post_id: int
    title: str
    disposition: str
    post_kind: str


@dataclass(frozen=True)
class SelectionReview:
    gallery_id: str
    pages: tuple[int, ...]
    rows: tuple[SelectionRow, ...]
    complete: bool


class ManualSelection:
    def __init__(self):
        self.generation = 0
        self.gallery_id = ""
        self.pages = ()
        self.rows = {}
        self.selected = set()
        self.complete = False

    def invalidate(self):
        self.generation += 1
        self.gallery_id = ""
        self.pages = ()
        self.rows = {}
        self.selected.clear()
        self.complete = False

    def begin(self, gallery_id, pages):
        self.invalidate()
        validate_gallery(gallery_id)
        pages = tuple(pages)
        if any(type(page) is not int for page in pages):
            raise ValueError("페이지 번호 오류")
        parse_pages(" ".join(map(str, pages)))
        self.gallery_id, self.pages = gallery_id, pages
        return self.generation

    def apply(self, generation, result):
        if generation != self.generation:
            return False
        if (result.get("gallery_id") != self.gallery_id
                or tuple(result.get("requested_pages", ())) != self.pages):
            raise ValueError("조회 범위 불일치")
        rows = {}
        for item in result["items"]:
            post_id = validate_post_id(item["post_id"])
            if (post_id in rows or not isinstance(item["title"], str)
                    or item["disposition"] not in {
                        "delete_guest", "protected_member", "unknown_author", "conflicting_metadata"}
                    or item["post_kind"] not in {"ordinary", "recommended", "unknown", "conflicting"}):
                raise ValueError("목록 자료 오류")
            rows[post_id] = SelectionRow(post_id, item["title"], item["disposition"], item["post_kind"])
        self.rows = rows
        self.selected.clear()
        self.complete = result.get("requested_pages_completed") is True
        return True

    def select(self, mode):
        if mode == "guests":
            self.selected = {key for key, row in self.rows.items() if row.disposition == "delete_guest"}
        elif mode == "all":
            self.selected = set(self.rows)
        elif mode == "none":
            self.selected.clear()
        else:
            raise ValueError("선택 방식 오류")

    def toggle(self, post_id):
        if post_id not in self.rows:
            return
        self.selected.symmetric_difference_update({post_id})

    def review(self):
        return SelectionReview(self.gallery_id, self.pages,
                               tuple(row for key, row in self.rows.items() if key in self.selected),
                               self.complete)
