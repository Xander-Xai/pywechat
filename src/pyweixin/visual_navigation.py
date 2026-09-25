"""Fail-closed visual navigation for WeChat 4.x contact read-only flows.

All screen coordinates are normalized against the Win32 client rectangle.
Screenshots stay in memory. This module never prints OCR text or contact data.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageFilter, ImageStat


WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
WINDOW_NOT_READY = "WINDOW_NOT_READY"
NAVIGATION_UNRESOLVED = "NAVIGATION_UNRESOLVED"
CONTACTS_PAGE_NOT_CONFIRMED = "CONTACTS_PAGE_NOT_CONFIRMED"
CONTACT_LIST_NOT_FOUND = "CONTACT_LIST_NOT_FOUND"
CONTACT_PROFILE_NOT_FOUND = "CONTACT_PROFILE_NOT_FOUND"
OCR_FAILED = "OCR_FAILED"

CONTACTS_NAV_ANCHOR = "CONTACTS_NAV_ANCHOR"
CONTACTS_NAV_REGION = (0.0, 0.19, 0.105, 0.40)
CONTACTS_NAV_CENTER = (0.047, 0.292)
CONTACTS_NAV_MIN_CONFIDENCE = 0.62
CONTACTS_LIST_REGION = (0.105, 0.20, 0.43, 0.98)
PROFILE_FALLBACK_REGION = (0.43, 0.035, 0.995, 0.94)
CONTACTS_PAGE_TERMS = (
    "\u65b0\u7684\u670b\u53cb",
    "\u7fa4\u804a",
    "\u516c\u4f17\u53f7",
    "\u4f01\u4e1a\u5fae\u4fe1",
    "New Friends",
    "Groups",
    "Official Accounts",
    "WeCom",
    "\u806f\u7d61\u4eba",
    "\u8054\u7cfb\u4eba",
    "Contacts",
)
CONTACTS_ROOT_TERMS = (
    "\u7fa4\u804a",
    "\u6807\u7b7e",
    "\u516c\u4f17\u53f7",
    "\u4f01\u4e1a\u5fae\u4fe1",
    "Group Chats",
    "Groups",
    "Tags",
    "Official Accounts",
    "WeCom Contacts",
    "\u7fa4\u7d44\u804a\u5929",
    "\u6a19\u7c64",
    "\u516c\u773e\u865f",
)
PROFILE_FIELD_TERMS = (
    "\u5fae\u4fe1\u53f7",
    "\u6635\u79f0",
    "\u5730\u533a",
    "\u5907\u6ce8",
    "\u7535\u8bdd",
    "\u6807\u7b7e",
    "\u4e2a\u6027\u7b7e\u540d",
    "\u6765\u6e90",
    "WeChat ID",
    "Weixin ID",
    "Nickname",
    "Region",
    "Mobile",
    "Tag",
    "Source",
)
CONTACTS_CATEGORY_TERMS = (
    "\u65b0\u7684\u670b\u53cb",
    "\u7fa4\u804a",
    "\u6807\u7b7e",
    "\u516c\u4f17\u53f7",
    "\u4f01\u4e1a\u5fae\u4fe1",
    "New Friends",
    "Group Chats",
    "Groups",
    "Tags",
    "Official Accounts",
    "WeCom Contacts",
    "\u65b0\u7684\u670b\u53cb",
    "\u7fa4\u7d44\u804a\u5929",
    "\u6a19\u7c64",
    "\u516c\u773e\u865f",
)


class NavigationFailure(RuntimeError):
    def __init__(self, status: str, detail: str = "") -> None:
        super().__init__(detail or status)
        self.status = status


@dataclass(frozen=True)
class NormalizedRect:
    left: float
    top: float
    right: float
    bottom: float

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


@dataclass(frozen=True)
class WindowCoordinateMapper:
    """Map normalized client coordinates to physical screen pixels."""

    client_screen_rect: tuple[int, int, int, int]
    window_screen_rect: tuple[int, int, int, int]
    dpi: int = 96

    @property
    def size(self) -> tuple[int, int]:
        left, top, right, bottom = self.client_screen_rect
        return right - left, bottom - top

    @property
    def scale(self) -> float:
        return self.dpi / 96.0

    def to_screen(self, x_ratio: float, y_ratio: float) -> tuple[int, int]:
        left, top, right, bottom = self.client_screen_rect
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise NavigationFailure(WINDOW_NOT_READY, "empty client rectangle")
        if not (0.0 <= x_ratio <= 1.0 and 0.0 <= y_ratio <= 1.0):
            raise ValueError("normalized coordinates must be within 0..1")
        return left + round(x_ratio * width), top + round(y_ratio * height)

    def to_normalized(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        left, top, right, bottom = self.client_screen_rect
        if right <= left or bottom <= top:
            raise NavigationFailure(WINDOW_NOT_READY, "empty client rectangle")
        return (screen_x - left) / (right - left), (screen_y - top) / (bottom - top)

    def map_rect(self, roi: NormalizedRect) -> tuple[int, int, int, int]:
        return (
            *self.to_screen(roi.left, roi.top),
            *self.to_screen(roi.right, roi.bottom),
        )


@dataclass(frozen=True)
class VisualAnchor:
    name: str = CONTACTS_NAV_ANCHOR
    region: NormalizedRect = NormalizedRect(*CONTACTS_NAV_REGION)
    center: tuple[float, float] = CONTACTS_NAV_CENTER
    text_hints: tuple[str, ...] = (
        "\u901a\u8baf\u5f55",
        "Contacts",
        "\u806f\u7d61\u4eba",
    )
    min_confidence: float = CONTACTS_NAV_MIN_CONFIDENCE


@dataclass(frozen=True)
class AnchorMatch:
    x_ratio: float
    y_ratio: float
    confidence: float
    method: str


@dataclass(frozen=True)
class PageEvidence:
    confirmed: bool
    method: str
    hint_count: int = 0
    latency_seconds: float = 0.0


def _normalized_box(box, width: int, height: int) -> NormalizedRect:
    return NormalizedRect(
        box[0] / width, box[1] / height, box[2] / width, box[3] / height
    )


def _gray_region_score(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Score a compact icon-like texture using spread-out edge evidence."""
    crop = image.convert("L").crop(box)
    if crop.width < 12 or crop.height < 12:
        return 0.0
    if max(ImageStat.Stat(crop).stddev) < 3.0:
        return 0.0
    edges = crop.filter(ImageFilter.FIND_EDGES)
    width, height = edges.size
    occupied = 0
    strength = 0.0
    for row in range(3):
        for col in range(3):
            cell = edges.crop(
                (
                    col * width // 3,
                    row * height // 3,
                    (col + 1) * width // 3,
                    (row + 1) * height // 3,
                )
            )
            mean = ImageStat.Stat(cell).mean[0] / 255.0
            strength += min(1.0, mean / 0.10)
            occupied += mean >= 0.018
    spread = occupied / 9.0
    return min(1.0, 0.55 * spread + 0.45 * (strength / 9.0))


def detect_contacts_anchor(
    image: Image.Image,
    tokens: Sequence[object] = (),
    anchor: VisualAnchor = VisualAnchor(),
    *,
    uia_hint: bool = False,
    uia_center: tuple[float, float] | None = None,
) -> AnchorMatch | None:
    """Find Contacts only within the configured left-rail ROI.

    OCR text is optional. Without it, the icon-like multi-cell evidence is
    combined with the known sidebar location and optional UIA hint.
    """
    width, height = image.size
    if width <= 0 or height <= 0:
        return None
    roi = anchor.region
    roi_px = (
        round(roi.left * width),
        round(roi.top * height),
        round(roi.right * width),
        round(roi.bottom * height),
    )
    label_tokens = []
    for token in tokens:
        text = str(getattr(token, "text", "")).casefold()
        if any(hint.casefold() in text for hint in anchor.text_hints):
            label_tokens.append(token)
    if label_tokens:
        # Only accept a label if its OCR box lies in the side-rail region.
        chosen = max(label_tokens, key=lambda item: getattr(item, "confidence", 0.0))
        box = getattr(chosen, "box", None)
        if box:
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            x = (min(xs) + max(xs)) / (2 * width)
            y = (min(ys) + max(ys)) / (2 * height)
            if roi.contains(x, y):
                if uia_center and not anchors_agree((x, y), uia_center):
                    return None
                return AnchorMatch(
                    x,
                    y,
                    min(1.0, float(getattr(chosen, "confidence", 0.0))),
                    "ocr_label",
                )
    # Geometry narrows the search; image texture across nine cells prevents a
    # click based on one pixel or a blank expected coordinate.
    icon_half_width = 0.026
    icon_half_height = 0.040
    icon_box = (
        round((anchor.center[0] - icon_half_width) * width),
        round((anchor.center[1] - icon_half_height) * height),
        round((anchor.center[0] + icon_half_width) * width),
        round((anchor.center[1] + icon_half_height) * height),
    )
    score = _gray_region_score(image, icon_box)
    layout_score = 1.0 if roi_px[2] <= round(width * 0.15) else 0.0
    uia_score = 0.08 if uia_hint else 0.0
    confidence = min(1.0, 0.76 * score + 0.16 * layout_score + uia_score)
    if uia_center and not anchors_agree(anchor.center, uia_center):
        return None
    if confidence < anchor.min_confidence:
        return None
    return AnchorMatch(
        anchor.center[0], anchor.center[1], confidence, "layout+icon_features"
    )


def anchors_agree(
    visual_center: tuple[float, float],
    uia_center: tuple[float, float],
    tolerance: tuple[float, float] = (0.04, 0.055),
) -> bool:
    """Reject UIA hints that point outside the visual anchor neighborhood."""
    return (
        abs(visual_center[0] - uia_center[0]) <= tolerance[0]
        and abs(visual_center[1] - uia_center[1]) <= tolerance[1]
    )


def confirm_contacts_page(
    tokens: Iterable[object],
    *,
    list_row_count: int = 0,
    uia_list_hint: bool = False,
    image_size: tuple[int, int] | None = None,
) -> PageEvidence:
    hints = 0
    width, height = image_size or (0, 0)
    for token in tokens:
        word = str(getattr(token, "text", "")).casefold()
        if not any(term.casefold() in word for term in CONTACTS_ROOT_TERMS):
            continue
        box = getattr(token, "box", None)
        if box and width > 0 and height > 0:
            x = sum(point[0] for point in box) / (4 * width)
            y = sum(point[1] for point in box) / (4 * height)
            if not NormalizedRect(*CONTACTS_LIST_REGION).contains(x, y):
                continue
        hints += 1
    if hints:
        return PageEvidence(True, "ocr_page_label", hints)
    if list_row_count >= 2 and uia_list_hint:
        return PageEvidence(True, "visual_list+uia_hint", list_row_count)
    return PageEvidence(False, "insufficient_page_evidence", hints)


def detect_contact_rows(
    tokens: Sequence[object],
    image_size: tuple[int, int],
    region: NormalizedRect = NormalizedRect(*CONTACTS_LIST_REGION),
) -> list[float]:
    """Return anonymized normalized row centers from OCR text boxes."""
    width, height = image_size
    if width <= 0 or height <= 0:
        return []
    centers = []
    category_bottoms = []
    eligible = []
    for token in tokens:
        box = getattr(token, "box", None)
        if not box:
            continue
        cx = sum(point[0] for point in box) / (4 * width)
        cy = sum(point[1] for point in box) / (4 * height)
        word = str(getattr(token, "text", "")).casefold()
        if region.contains(cx, cy) and any(
            term.casefold() in word for term in CONTACTS_CATEGORY_TERMS
        ):
            category_bottoms.append(max(point[1] for point in box) / height)
        eligible.append((token, box, cx, cy, word))
    minimum_y = max(region.top, 0.48)
    if category_bottoms:
        minimum_y = max(region.top, max(category_bottoms) + 0.02)
    for token, box, cx, cy, word in eligible:
        if any(term.casefold() in word for term in CONTACTS_PAGE_TERMS):
            continue
        if region.contains(cx, cy) and minimum_y <= cy <= 0.96:
            centers.append(cy)
    centers.sort()
    rows: list[float] = []
    for center in centers:
        if not rows or abs(center - rows[-1]) > 0.025:
            rows.append(center)
    return rows


class Win32VisualNavigation:
    """Win32 window-bound visual navigation; captures remain in memory."""

    def __init__(
        self,
        hwnd: int,
        *,
        anchor: VisualAnchor = VisualAnchor(),
        recognize: Callable[[Image.Image], Sequence[object]] | None = None,
        screenshot: Callable[[int], Image.Image] | None = None,
        click: Callable[[int, int], None] | None = None,
        foreground: Callable[[], int] | None = None,
        wait: Callable[[float], None] = time.sleep,
        mapper: WindowCoordinateMapper | None = None,
    ):
        self.hwnd = hwnd
        self.anchor = anchor
        self._recognize = recognize or self._shared_recognize
        self._screenshot = screenshot or self._capture_client
        self._click = click or self._click_once
        self._foreground = foreground or self._foreground_root
        self._wait = wait
        self._auto_mapper = mapper is None
        self.mapper = mapper or self._window_mapper()

    def _window_mapper(self) -> WindowCoordinateMapper:
        import win32gui
        import ctypes

        if not win32gui.IsWindow(self.hwnd):
            raise NavigationFailure(WINDOW_NOT_FOUND)
        win = win32gui.GetWindowRect(self.hwnd)
        client = win32gui.GetClientRect(self.hwnd)
        left_top = win32gui.ClientToScreen(self.hwnd, (client[0], client[1]))
        right_bottom = win32gui.ClientToScreen(self.hwnd, (client[2], client[3]))
        try:
            dpi = int(ctypes.windll.user32.GetDpiForWindow(self.hwnd)) or 96
        except Exception:
            dpi = 96
        return WindowCoordinateMapper(
            (left_top[0], left_top[1], right_bottom[0], right_bottom[1]),
            tuple(win),
            dpi,
        )

    @staticmethod
    def _foreground_root() -> int:
        import win32gui
        import win32con

        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetAncestor(hwnd, win32con.GA_ROOT) if hwnd else 0

    def _capture_client(self, _hwnd: int) -> Image.Image:
        from PIL import ImageGrab

        if self._auto_mapper:
            self.mapper = self._window_mapper()
        left, top, right, bottom = self.mapper.client_screen_rect
        image = ImageGrab.grab(
            bbox=(left, top, right, bottom), all_screens=True
        ).convert("RGB")
        if image.size != self.mapper.size:
            raise NavigationFailure(
                WINDOW_NOT_READY, "screenshot and client geometry differ"
            )
        return image

    @staticmethod
    def _click_once(x: int, y: int) -> None:
        import win32api
        import win32con

        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    @staticmethod
    def _shared_recognize(image: Image.Image) -> Sequence[object]:
        from wechat_ocr.config import DEFAULT_CONFIG
        from wechat_ocr.engine import get_ocr_engine

        return get_ocr_engine(DEFAULT_CONFIG).recognize(image)

    def _anchor(
        self, image: Image.Image, *, uia_hint: bool = False
    ) -> AnchorMatch | None:
        tokens = self._tokens(image)
        return detect_contacts_anchor(image, tokens, self.anchor, uia_hint=uia_hint)

    def _tokens(self, image: Image.Image) -> Sequence[object]:
        try:
            return self._recognize(image)
        except Exception as exc:
            raise NavigationFailure(OCR_FAILED, type(exc).__name__) from exc

    def _validate_click(
        self, x_ratio: float, y_ratio: float, region: NormalizedRect
    ) -> tuple[int, int]:
        if self._foreground() != self.hwnd:
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "target window is not foreground"
            )
        if not region.contains(x_ratio, y_ratio):
            raise NavigationFailure(NAVIGATION_UNRESOLVED, "click outside expected ROI")
        if not NormalizedRect(0, 0, 1, 1).contains(x_ratio, y_ratio):
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "click outside client rectangle"
            )
        return self.mapper.to_screen(x_ratio, y_ratio)

    def open_contacts(
        self, *, uia_hint: bool = False, uia_list_hint: bool = False
    ) -> PageEvidence:
        started = time.perf_counter()
        if self._foreground() != self.hwnd:
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "target window is not foreground"
            )
        before = self._screenshot(self.hwnd)
        match = self._anchor(before, uia_hint=uia_hint)
        if match is None or match.confidence < self.anchor.min_confidence:
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "contacts visual anchor not verified"
            )
        # Re-capture and match a second time immediately before the one click.
        current = self._screenshot(self.hwnd)
        current_match = self._anchor(current, uia_hint=uia_hint)
        if current_match is None or current_match.method != match.method:
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "anchor changed before click"
            )
        if (
            abs(current_match.x_ratio - match.x_ratio) > 0.02
            or abs(current_match.y_ratio - match.y_ratio) > 0.03
        ):
            raise NavigationFailure(NAVIGATION_UNRESOLVED, "anchor moved before click")
        click_region = self.anchor.region
        point = self._validate_click(
            current_match.x_ratio, current_match.y_ratio, click_region
        )
        self._click(*point)
        self._wait(0.8)
        after = self._screenshot(self.hwnd)
        if image_fingerprint(after) == image_fingerprint(current):
            raise NavigationFailure(
                CONTACTS_PAGE_NOT_CONFIRMED, "screenshot did not change"
            )
        tokens = self._tokens(after)
        rows = detect_contact_rows(tokens, after.size)
        page = confirm_contacts_page(
            tokens,
            list_row_count=len(rows),
            uia_list_hint=uia_list_hint,
            image_size=after.size,
        )
        if not page.confirmed:
            raise NavigationFailure(CONTACTS_PAGE_NOT_CONFIRMED)
        if not rows and not uia_list_hint:
            raise NavigationFailure(CONTACT_LIST_NOT_FOUND)
        return PageEvidence(
            page.confirmed,
            page.method,
            page.hint_count,
            time.perf_counter() - started,
        )

    def confirm_current_contacts(self, *, uia_list_hint: bool = False) -> PageEvidence:
        """Verify an already-open Contacts page without clicking anything."""
        started = time.perf_counter()
        image = self._screenshot(self.hwnd)
        tokens = self._tokens(image)
        rows = detect_contact_rows(tokens, image.size)
        page = confirm_contacts_page(
            tokens,
            list_row_count=len(rows),
            uia_list_hint=uia_list_hint,
            image_size=image.size,
        )
        if not page.confirmed:
            raise NavigationFailure(CONTACTS_PAGE_NOT_CONFIRMED)
        if not rows and not uia_list_hint:
            raise NavigationFailure(CONTACT_LIST_NOT_FOUND)
        return PageEvidence(
            page.confirmed,
            page.method,
            page.hint_count,
            time.perf_counter() - started,
        )

    def contact_rows(self, *, page_verified: bool) -> list[float]:
        if not page_verified:
            raise NavigationFailure(CONTACTS_PAGE_NOT_CONFIRMED)
        image = self._screenshot(self.hwnd)
        return detect_contact_rows(self._tokens(image), image.size)

    def select_contact(
        self,
        contact_index: int,
        *,
        page_verified: bool,
        smoke_limit: int = 5,
        uia_row_hint: bool = False,
    ) -> float:
        if contact_index < 1 or contact_index > smoke_limit:
            raise NavigationFailure(
                CONTACT_LIST_NOT_FOUND, "contact index exceeds read-only smoke bound"
            )
        rows = self.contact_rows(page_verified=page_verified)
        if contact_index > len(rows):
            raise NavigationFailure(CONTACT_LIST_NOT_FOUND)
        y_ratio = rows[contact_index - 1]
        x_ratio = (CONTACTS_LIST_REGION[0] + CONTACTS_LIST_REGION[2]) / 2
        region = NormalizedRect(*CONTACTS_LIST_REGION)
        # Fresh screenshot and OCR row revalidation before a single click.
        fresh = self._screenshot(self.hwnd)
        fresh_rows = detect_contact_rows(self._tokens(fresh), fresh.size)
        if (
            len(fresh_rows) < contact_index
            or abs(fresh_rows[contact_index - 1] - y_ratio) > 0.025
        ):
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "contact row shifted before click"
            )
        point = self._validate_click(x_ratio, y_ratio, region)
        self._click(*point)
        self._wait(0.8)
        selected = self._screenshot(self.hwnd)
        if image_fingerprint(selected) == image_fingerprint(fresh):
            raise NavigationFailure(
                CONTACT_PROFILE_NOT_FOUND, "profile view did not change"
            )
        return y_ratio

    def profile_image(
        self,
        *,
        page_verified: bool,
        uia_rect: tuple[int, int, int, int] | None = None,
    ) -> tuple[Image.Image, float]:
        if not page_verified:
            raise NavigationFailure(CONTACTS_PAGE_NOT_CONFIRMED)
        started = time.perf_counter()
        image = self._screenshot(self.hwnd)
        width, height = image.size
        box = None
        if uia_rect:
            try:
                left, top = self.mapper.to_normalized(uia_rect[0], uia_rect[1])
                right, bottom = self.mapper.to_normalized(uia_rect[2], uia_rect[3])
                if (
                    right > left
                    and bottom > top
                    and left >= 0.38
                    and right <= 1.0
                    and top >= 0.0
                    and bottom <= 1.0
                    and right - left >= 0.12
                    and bottom - top >= 0.18
                ):
                    box = (
                        round(left * width),
                        round(top * height),
                        round(right * width),
                        round(bottom * height),
                    )
            except (TypeError, ValueError, IndexError):
                box = None
        if box is None:
            box = (
                round(PROFILE_FALLBACK_REGION[0] * width),
                round(PROFILE_FALLBACK_REGION[1] * height),
                round(PROFILE_FALLBACK_REGION[2] * width),
                round(PROFILE_FALLBACK_REGION[3] * height),
            )
        crop = image.crop(box)
        if crop.width < 16 or crop.height < 16:
            raise NavigationFailure(CONTACT_PROFILE_NOT_FOUND)
        tokens = self._tokens(crop)
        matched_profile_labels = {
            term.casefold()
            for term in PROFILE_FIELD_TERMS
            if any(
                term.casefold() in str(getattr(token, "text", "")).casefold()
                for token in tokens
            )
        }
        has_id_label = any(
            term.casefold() in matched_profile_labels
            for term in ("\u5fae\u4fe1\u53f7", "WeChat ID", "Weixin ID")
        )
        feature_score = _gray_region_score(crop, (0, 0, crop.width, crop.height))
        if (
            not has_id_label
            and len(matched_profile_labels) < 2
            and feature_score < 0.16
        ):
            raise NavigationFailure(CONTACT_PROFILE_NOT_FOUND)
        return crop, time.perf_counter() - started

    def extract_selected_profile(
        self,
        *,
        page_verified: bool,
        uia_rect: tuple[int, int, int, int] | None = None,
    ):
        """Capture the profile ROI and call the existing shared ID extractor."""
        from wechat_ocr import DEFAULT_CONFIG, WeChatIdExtractor
        from wechat_ocr.extractor import reconcile_ocr_result

        image, capture_latency = self.profile_image(
            page_verified=page_verified, uia_rect=uia_rect
        )
        started = time.perf_counter()
        ocr = WeChatIdExtractor(config=DEFAULT_CONFIG).extract(image)
        ocr_latency = time.perf_counter() - started
        # UIA remains optional and is only read after the OCR result exists.
        return reconcile_ocr_result(ocr, None), capture_latency, ocr_latency


def image_fingerprint(image: Image.Image) -> str:
    """In-memory change evidence only; never writes an image artifact."""
    return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


class VisualContactSession:
    """A verified Contacts-page session for bounded, read-only OCR smoke."""

    visual_navigation = True

    def __init__(self, navigator: Win32VisualNavigation, page_evidence: PageEvidence):
        self.navigator = navigator
        self.page_evidence = page_evidence

    def read_contacts(self, max_contacts: int = 5) -> list[dict[str, object]]:
        if not 1 <= max_contacts <= 5:
            raise ValueError("visual read-only smoke is limited to 1..5 contacts")
        rows = self.navigator.contact_rows(page_verified=self.page_evidence.confirmed)
        if not rows:
            raise NavigationFailure(CONTACT_LIST_NOT_FOUND)
        results = []
        for contact_index in range(1, min(max_contacts, len(rows)) + 1):
            contact_started = time.perf_counter()
            self.navigator.select_contact(
                contact_index,
                page_verified=self.page_evidence.confirmed,
                smoke_limit=5,
            )
            result, capture_latency, ocr_latency = (
                self.navigator.extract_selected_profile(
                    page_verified=self.page_evidence.confirmed
                )
            )
            results.append(
                {
                    "contact_index": contact_index,
                    "navigation_method": "visual",
                    "contacts_page_verified": self.page_evidence.confirmed,
                    "navigation_latency_seconds": self.page_evidence.latency_seconds,
                    "profile_found": True,
                    "微信号": result.value if result.is_confirmed else "无",
                    "微信号状态": result.status,
                    "微信号来源": result.source,
                    "微信号置信度": result.confidence,
                    "微信号原因": result.reason or "",
                    "微信号候选": result.value if not result.is_confirmed else "",
                    "ocr_found": any(result.ocr_candidates),
                    "uia_found": result.ui_candidate is not None,
                    "agreement": (
                        "match"
                        if result.ui_candidate == result.value
                        else "unavailable"
                        if result.ui_candidate is None
                        else "mismatch"
                    ),
                    "capture_latency_seconds": capture_latency,
                    "ocr_latency_seconds": ocr_latency,
                    "total_latency_seconds": (
                        self.page_evidence.latency_seconds
                        + time.perf_counter()
                        - contact_started
                    ),
                }
            )
        return results
