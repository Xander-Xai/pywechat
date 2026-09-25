"""Fail-closed visual navigation for WeChat 4.x contact read-only flows.

All screen coordinates are normalized against the Win32 client rectangle.
Screenshots stay in memory. This module never prints OCR text or contact data.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageChops, ImageFilter, ImageStat


WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
WINDOW_NOT_READY = "WINDOW_NOT_READY"
NAVIGATION_UNRESOLVED = "NAVIGATION_UNRESOLVED"
CONTACTS_PAGE_NOT_CONFIRMED = "CONTACTS_PAGE_NOT_CONFIRMED"
CONTACT_LIST_NOT_FOUND = "CONTACT_LIST_NOT_FOUND"
CONTACT_PROFILE_NOT_FOUND = "CONTACT_PROFILE_NOT_FOUND"
OCR_FAILED = "OCR_FAILED"
PROFILE_NOT_STABLE = "PROFILE_NOT_STABLE"
PROFILE_POPUP_OPENED = "PROFILE_POPUP_OPENED"
PROFILE_POPUP_NOT_OPENED = "PROFILE_POPUP_NOT_OPENED"
PROFILE_POPUP_UNRESOLVED = "PROFILE_POPUP_UNRESOLVED"
WINDOW_FOREGROUND_REJECTED = "WINDOW_FOREGROUND_REJECTED"
AUTO_FOREGROUND_READY = "AUTO_FOREGROUND_READY"
AUTO_FOREGROUND_REJECTED = "AUTO_FOREGROUND_REJECTED"
OPERATOR_FOREGROUND_REQUIRED = "OPERATOR_FOREGROUND_REQUIRED"

PROFILE_STABLE_WAIT_MS = 300
PROFILE_STABLE_TIMEOUT_MS = 5000
PROFILE_STABLE_MEAN_DIFF = 1.4

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


@dataclass(frozen=True)
class FriendProfileSurface:
    source: str
    hwnd: int | None
    bounds: tuple[int, int, int, int]
    confidence: float
    verification_signals: tuple[str, ...]
    image: Image.Image


def focus_window_with_bounded_retry(
    hwnd: int,
    *,
    is_iconic: Callable[[int], bool],
    show_restore: Callable[[int], object],
    bring_to_top: Callable[[int], object],
    set_foreground: Callable[[int], object],
    get_foreground: Callable[[], int],
    wait: Callable[[float], None] = time.sleep,
    attempts: int = 3,
) -> bool:
    """Restore and foreground a window with a strict retry and delay bound."""
    for attempt in range(min(3, max(1, attempts))):
        try:
            if is_iconic(hwnd):
                show_restore(hwnd)
            bring_to_top(hwnd)
            set_foreground(hwnd)
            if get_foreground() == hwnd:
                return True
        except Exception:
            pass
        if attempt + 1 < min(3, max(1, attempts)):
            wait(0.12)
    return False


def detect_friend_avatar_anchor(
    profile_image: Image.Image, tokens: Sequence[object]
) -> AnchorMatch | None:
    """Derive one avatar point from adjacent OCR geometry and image texture."""
    width, height = profile_image.size
    candidates = []
    for token in tokens:
        box = getattr(token, "box", None)
        if not box:
            continue
        left = min(point[0] for point in box)
        top = min(point[1] for point in box)
        bottom = max(point[1] for point in box)
        center_y = (top + bottom) / 2
        if 0.08 * height <= center_y <= 0.55 * height and left >= 0.16 * width:
            candidates.append(
                (top, left, center_y, float(getattr(token, "confidence", 0.0)))
            )
    if not candidates:
        return None
    _, text_left, center_y, text_confidence = min(candidates)
    avatar_size = max(28, round(min(width, height) * 0.13))
    center_x = text_left - avatar_size * 0.63
    bounds = (
        round(center_x - avatar_size / 2),
        round(center_y - avatar_size / 2),
        round(center_x + avatar_size / 2),
        round(center_y + avatar_size / 2),
    )
    if (
        bounds[0] < 0
        or bounds[1] < 0
        or bounds[2] > width
        or bounds[3] > height
        or center_x > text_left - avatar_size * 0.3
    ):
        return None
    texture = _gray_region_score(profile_image, bounds)
    layout_score = 1.0 if 0.08 * width <= center_x <= 0.78 * width else 0.0
    confidence = min(1.0, 0.58 * texture + 0.24 * text_confidence + 0.18 * layout_score)
    if texture < 0.18 or confidence < 0.50:
        return None
    return AnchorMatch(
        center_x / width,
        center_y / height,
        confidence,
        "profile_text+avatar_texture",
    )


def select_popup_window(
    windows: Sequence[dict[str, object]], main_hwnd: int
) -> dict[str, object] | None:
    """Choose a visible Weixin profile popup owned by the target main HWND."""
    for window in windows:
        if not window.get("visible") or window.get("hwnd") == main_hwnd:
            continue
        cls = str(window.get("class_name", ""))
        title = str(window.get("title", ""))
        owner = int(window.get("owner", 0) or 0)
        if cls == "mmui::ProfileUniquePop" and (
            owner == main_hwnd or title == "Weixin"
        ):
            return window
    return None


def has_profile_content(tokens: Sequence[object]) -> bool:
    """Require multiple recognized profile fields before accepting a popup."""
    recognized = {
        term.casefold()
        for term in PROFILE_FIELD_TERMS
        if any(
            term.casefold() in str(getattr(token, "text", "")).casefold()
            for token in tokens
        )
    }
    return len(recognized) >= 2


def detect_popup_diff_bounds(
    before: Image.Image, after: Image.Image
) -> tuple[tuple[int, int, int, int], float] | None:
    """Estimate a centered same-HWND popup from high-contrast frame deltas."""
    if before.size != after.size or before.width < 160 or before.height < 120:
        return None
    delta = ImageChops.difference(before.convert("L"), after.convert("L"))
    tile = max(8, min(before.size) // 32)
    samples = []
    points = []
    for top in range(0, before.height, tile):
        for left in range(0, before.width, tile):
            right = min(before.width, left + tile)
            bottom = min(before.height, top + tile)
            mean = ImageStat.Stat(delta.crop((left, top, right, bottom))).mean[0]
            samples.append(mean)
            points.append((left, top, right, bottom, mean))
    median = sorted(samples)[len(samples) // 2]
    threshold = max(24.0, median + 18.0)
    active = [item for item in points if item[4] >= threshold]
    if len(active) < 12:
        return None
    left = min(item[0] for item in active)
    top = min(item[1] for item in active)
    right = max(item[2] for item in active)
    bottom = max(item[3] for item in active)
    box_width, box_height = right - left, bottom - top
    if not (0.18 <= box_width / before.width <= 0.78):
        return None
    if not (0.16 <= box_height / before.height <= 0.82):
        return None
    if not (0.20 <= (left + right) / (2 * before.width) <= 0.80):
        return None
    density = len(active) / max(
        1, ((box_width + tile - 1) // tile) * ((box_height + tile - 1) // tile)
    )
    if density < 0.24:
        return None
    confidence = min(1.0, 0.48 + 0.45 * density)
    return (left, top, right, bottom), confidence


def foreground_wechat_window(
    hwnd: int, wait: Callable[[float], None] = time.sleep
) -> bool:
    """Win32 foreground helper with a bounded restore/raise/verify cycle."""
    import win32con
    import win32gui

    def foreground_root() -> int:
        current = win32gui.GetForegroundWindow()
        return win32gui.GetAncestor(current, win32con.GA_ROOT) if current else 0

    if foreground_root() == hwnd:
        return True
    return focus_window_with_bounded_retry(
        hwnd,
        is_iconic=win32gui.IsIconic,
        show_restore=lambda target: win32gui.ShowWindow(target, win32con.SW_RESTORE),
        bring_to_top=win32gui.BringWindowToTop,
        set_foreground=win32gui.SetForegroundWindow,
        get_foreground=foreground_root,
        wait=wait,
        attempts=3,
    )


def foreground_acquisition_state(
    *,
    target_is_foreground: bool,
    auto_request_succeeded: bool,
    operator_fallback_enabled: bool = False,
) -> str:
    """Return an explicit navigation state, separate from all OCR statuses."""
    if target_is_foreground or auto_request_succeeded:
        return AUTO_FOREGROUND_READY
    return (
        OPERATOR_FOREGROUND_REQUIRED
        if operator_fallback_enabled
        else AUTO_FOREGROUND_REJECTED
    )


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
        popup_windows: Callable[[], Sequence[dict[str, object]]] | None = None,
        capture_window: Callable[[int], Image.Image] | None = None,
        wait: Callable[[float], None] = time.sleep,
        mapper: WindowCoordinateMapper | None = None,
    ):
        self.hwnd = hwnd
        self.anchor = anchor
        self._recognize = recognize or self._shared_recognize
        self._screenshot = screenshot or self._capture_client
        self._click = click or self._click_once
        self._foreground = foreground or self._foreground_root
        self._popup_windows = popup_windows or self._enumerate_profile_popups
        self._capture_window = capture_window or self._capture_window_client
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
        started = time.perf_counter()
        image = self._screenshot(self.hwnd)
        rows = detect_contact_rows(self._tokens(image), image.size)
        self.last_contact_row_detect_seconds = time.perf_counter() - started
        return rows

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
        row_detection_started = time.perf_counter()
        rows = self.contact_rows(page_verified=page_verified)
        if contact_index > len(rows):
            raise NavigationFailure(CONTACT_LIST_NOT_FOUND)
        y_ratio = rows[contact_index - 1]
        x_ratio = (CONTACTS_LIST_REGION[0] + CONTACTS_LIST_REGION[2]) / 2
        region = NormalizedRect(*CONTACTS_LIST_REGION)
        # Fresh screenshot and OCR row revalidation before a single click.
        fresh = self._screenshot(self.hwnd)
        fresh_rows = detect_contact_rows(self._tokens(fresh), fresh.size)
        row_detection_seconds = time.perf_counter() - row_detection_started
        if (
            len(fresh_rows) < contact_index
            or abs(fresh_rows[contact_index - 1] - y_ratio) > 0.025
        ):
            raise NavigationFailure(
                NAVIGATION_UNRESOLVED, "contact row shifted before click"
            )
        point = self._validate_click(x_ratio, y_ratio, region)
        click_started = time.perf_counter()
        self._click(*point)
        click_latency = time.perf_counter() - click_started
        selected, stability_latency = self.wait_profile_stable()
        if image_fingerprint(selected) == image_fingerprint(fresh):
            raise NavigationFailure(
                CONTACT_PROFILE_NOT_FOUND, "profile view did not change"
            )
        self.last_profile_stability_seconds = stability_latency
        self.last_contact_timing = {
            "contact_row_detect": row_detection_seconds,
            "contact_click": click_latency,
            "profile_stability_wait": stability_latency,
        }
        return y_ratio

    @staticmethod
    def _enumerate_profile_popups() -> Sequence[dict[str, object]]:
        import win32gui

        windows = []

        def collect(hwnd, _context):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                windows.append(
                    {
                        "hwnd": hwnd,
                        "owner": win32gui.GetWindow(hwnd, 4),
                        "title": win32gui.GetWindowText(hwnd),
                        "class_name": win32gui.GetClassName(hwnd),
                        "visible": True,
                    }
                )
            except Exception:
                pass
            return True

        win32gui.EnumWindows(collect, None)
        return windows

    @staticmethod
    def _capture_window_client(hwnd: int) -> Image.Image:
        import win32gui
        from PIL import ImageGrab

        client = win32gui.GetClientRect(hwnd)
        upper_left = win32gui.ClientToScreen(hwnd, (client[0], client[1]))
        lower_right = win32gui.ClientToScreen(hwnd, (client[2], client[3]))
        return ImageGrab.grab(
            bbox=(*upper_left, *lower_right), all_screens=True
        ).convert("RGB")

    def _wait_surface_stable(
        self, surface: FriendProfileSurface, timeout_ms: int = 3000
    ) -> FriendProfileSurface:
        started = time.perf_counter()
        deadline = started + timeout_ms / 1000
        previous = None
        stable = 0
        while time.perf_counter() < deadline:
            current = (
                self._capture_window(surface.hwnd)
                if surface.hwnd
                else self._screenshot(self.hwnd).crop(surface.bounds)
            )
            if previous is not None and previous.size == current.size:
                delta = ImageStat.Stat(
                    ImageChops.difference(previous.convert("L"), current.convert("L"))
                ).mean[0]
                stable = stable + 1 if delta <= PROFILE_STABLE_MEAN_DIFF else 0
                if stable >= 2:
                    return FriendProfileSurface(
                        surface.source,
                        surface.hwnd,
                        surface.bounds,
                        surface.confidence,
                        (*surface.verification_signals, "stable_frames"),
                        current,
                    )
            previous = current
            self._wait(PROFILE_STABLE_WAIT_MS / 1000)
        raise NavigationFailure(PROFILE_NOT_STABLE, "popup surface did not settle")

    def open_friend_profile_visual(
        self,
        *,
        page_verified: bool,
        profile_crop: Image.Image | None = None,
        profile_tokens: Sequence[object] | None = None,
    ) -> FriendProfileSurface:
        """Open and verify one Friend Profile Popup from the selected detail card."""
        if not page_verified:
            raise NavigationFailure(CONTACTS_PAGE_NOT_CONFIRMED)
        if self._foreground() != self.hwnd:
            raise NavigationFailure(OPERATOR_FOREGROUND_REQUIRED)
        started = time.perf_counter()
        self.last_popup_timing = {}
        self.last_popup_status = PROFILE_POPUP_UNRESOLVED
        before = self._screenshot(self.hwnd)
        if profile_crop is None:
            profile_crop, _ = self.profile_image(page_verified=True, image=before)
        tokens = (
            profile_tokens
            if profile_tokens is not None
            else getattr(self, "last_profile_tokens", ())
        )
        anchor = detect_friend_avatar_anchor(profile_crop, tokens)
        if anchor is None or anchor.confidence < 0.50:
            self.last_popup_status = PROFILE_POPUP_UNRESOLVED
            self.last_popup_timing = {
                "popup_anchor": time.perf_counter() - started,
                "popup_open": 0.0,
                "popup_stability_wait": 0.0,
            }
            raise NavigationFailure(
                PROFILE_POPUP_UNRESOLVED, "avatar anchor unresolved"
            )

        # Re-capture and recompute the one OCR-derived target immediately
        # before clicking; a moved or ambiguous anchor fails closed.
        current = self._screenshot(self.hwnd)
        current_crop, _ = self.profile_image(page_verified=True, image=current)
        current_anchor = detect_friend_avatar_anchor(
            current_crop, getattr(self, "last_profile_tokens", tokens)
        )
        if (
            current_anchor is None
            or anchor.method != current_anchor.method
            or not anchors_agree(
                (anchor.x_ratio, anchor.y_ratio),
                (current_anchor.x_ratio, current_anchor.y_ratio),
            )
        ):
            self.last_popup_status = PROFILE_POPUP_UNRESOLVED
            self.last_popup_timing = {
                "popup_anchor": time.perf_counter() - started,
                "popup_open": 0.0,
                "popup_stability_wait": 0.0,
            }
            raise NavigationFailure(PROFILE_POPUP_UNRESOLVED, "avatar anchor shifted")

        left, top, right, bottom = PROFILE_FALLBACK_REGION
        x_ratio = left + current_anchor.x_ratio * (right - left)
        y_ratio = top + current_anchor.y_ratio * (bottom - top)
        click_region = NormalizedRect(left, top, right, 0.62)
        point = self._validate_click(x_ratio, y_ratio, click_region)
        old_popups = {int(item.get("hwnd", 0)) for item in self._popup_windows()}
        click_started = time.perf_counter()
        popup_anchor_latency = click_started - started
        self._click(*point)

        unresolved_diff = None
        for _ in range(7):
            self._wait(0.3)
            new_popup = next(
                (
                    item
                    for item in self._popup_windows()
                    if int(item.get("hwnd", 0)) not in old_popups
                    and select_popup_window([item], self.hwnd) is not None
                ),
                None,
            )
            if new_popup:
                popup_detected_at = time.perf_counter()
                popup_hwnd = int(new_popup["hwnd"])
                rect = self._window_rect(popup_hwnd)
                raw_image = self._capture_window(popup_hwnd)
                surface = FriendProfileSurface(
                    "popup_hwnd",
                    popup_hwnd,
                    rect,
                    0.88,
                    ("owned_profile_popup_hwnd",),
                    raw_image,
                )
                stable_surface = self._wait_surface_stable(surface)
                popup_tokens = self._tokens(stable_surface.image)
                if (
                    not has_profile_content(popup_tokens)
                    or stable_surface.confidence < 0.60
                ):
                    self.last_popup_status = PROFILE_POPUP_UNRESOLVED
                    raise NavigationFailure(
                        PROFILE_POPUP_UNRESOLVED, "popup lacks profile text evidence"
                    )
                self.last_popup_status = PROFILE_POPUP_OPENED
                self.last_popup_timing = {
                    "popup_anchor": popup_anchor_latency,
                    "popup_open": popup_detected_at - click_started,
                    "popup_stability_wait": time.perf_counter() - popup_detected_at,
                    "total": time.perf_counter() - started,
                }
                return stable_surface

            after = self._screenshot(self.hwnd)
            if image_fingerprint(after) != image_fingerprint(current):
                diff = detect_popup_diff_bounds(current, after)
                if diff:
                    popup_detected_at = time.perf_counter()
                    bounds, confidence = diff
                    crop = after.crop(bounds)
                    if confidence >= 0.60 and has_profile_content(self._tokens(crop)):
                        unresolved_diff = FriendProfileSurface(
                            "screenshot_diff",
                            None,
                            bounds,
                            confidence,
                            ("same_hwnd_visual_bounds", "profile_text_density"),
                            crop,
                        )
                        self.last_popup_detected_at = popup_detected_at
                        break
                    unresolved_diff = FriendProfileSurface(
                        "screenshot_diff",
                        None,
                        bounds,
                        confidence,
                        ("same_hwnd_visual_bounds",),
                        crop,
                    )

        if (
            unresolved_diff
            and "profile_text_density" in unresolved_diff.verification_signals
        ):
            stable_surface = self._wait_surface_stable(unresolved_diff)
            self.last_popup_status = PROFILE_POPUP_OPENED
            self.last_popup_timing = {
                "popup_anchor": popup_anchor_latency,
                "popup_open": self.last_popup_detected_at - click_started,
                "popup_stability_wait": time.perf_counter()
                - self.last_popup_detected_at,
                "total": time.perf_counter() - started,
            }
            return stable_surface
        if unresolved_diff:
            self.last_popup_status = PROFILE_POPUP_UNRESOLVED
            self.last_popup_timing = {
                "popup_anchor": popup_anchor_latency,
                "popup_open": time.perf_counter() - click_started,
                "popup_stability_wait": 0.0,
            }
            raise NavigationFailure(
                PROFILE_POPUP_UNRESOLVED, "popup diff lacks text evidence"
            )
        self.last_popup_status = PROFILE_POPUP_NOT_OPENED
        self.last_popup_timing = {
            "popup_anchor": popup_anchor_latency,
            "popup_open": time.perf_counter() - click_started,
            "popup_stability_wait": 0.0,
        }
        raise NavigationFailure(PROFILE_POPUP_NOT_OPENED, "no popup surface verified")

    @staticmethod
    def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
        import win32gui

        return tuple(win32gui.GetWindowRect(hwnd))

    def wait_profile_stable(self) -> tuple[Image.Image, float]:
        """Poll in-memory right-panel frames until two consecutive deltas settle."""
        started = time.perf_counter()
        deadline = started + PROFILE_STABLE_TIMEOUT_MS / 1000
        previous = None
        consecutive_stable = 0
        current = self._screenshot(self.hwnd)
        while time.perf_counter() < deadline:
            self._wait(PROFILE_STABLE_WAIT_MS / 1000)
            current = self._screenshot(self.hwnd)
            if previous is not None:
                width, height = current.size
                box = (
                    round(PROFILE_FALLBACK_REGION[0] * width),
                    round(PROFILE_FALLBACK_REGION[1] * height),
                    round(PROFILE_FALLBACK_REGION[2] * width),
                    round(PROFILE_FALLBACK_REGION[3] * height),
                )
                old = previous.crop(box).convert("L")
                new = current.crop(box).convert("L")
                mean_diff = ImageStat.Stat(ImageChops.difference(old, new)).mean[0]
                consecutive_stable = (
                    consecutive_stable + 1
                    if mean_diff <= PROFILE_STABLE_MEAN_DIFF
                    else 0
                )
                if consecutive_stable >= 2:
                    self.last_stable_frame = current
                    return current, time.perf_counter() - started
            previous = current
        raise NavigationFailure(
            PROFILE_NOT_STABLE,
            f"profile ROI did not settle within {PROFILE_STABLE_TIMEOUT_MS}ms",
        )

    def profile_image(
        self,
        *,
        page_verified: bool,
        uia_rect: tuple[int, int, int, int] | None = None,
        image: Image.Image | None = None,
    ) -> tuple[Image.Image, float]:
        if not page_verified:
            raise NavigationFailure(CONTACTS_PAGE_NOT_CONFIRMED)
        started = time.perf_counter()
        image = image if image is not None else self._screenshot(self.hwnd)
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
        self.last_profile_tokens = tokens
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

    def profile_rois(
        self, image: Image.Image | None = None
    ) -> tuple[dict[str, Image.Image], float]:
        """Return bounded A/B/C profile regions from one in-memory frame."""
        started = time.perf_counter()
        frame = image or self._screenshot(self.hwnd)
        width, height = frame.size
        normalized = {
            "original": (0.43, 0.035, 0.995, 0.94),
            "expanded": (0.37, 0.015, 0.999, 0.98),
            "right_panel": (0.34, 0.0, 1.0, 1.0),
        }
        crops = {
            name: frame.crop(
                (
                    round(left_ratio * width),
                    round(top_ratio * height),
                    round(right_ratio * width),
                    round(bottom_ratio * height),
                )
            )
            for name, (
                left_ratio,
                top_ratio,
                right_ratio,
                bottom_ratio,
            ) in normalized.items()
        }
        return crops, time.perf_counter() - started

    def extract_selected_profile(
        self,
        *,
        page_verified: bool,
        uia_rect: tuple[int, int, int, int] | None = None,
        uia_candidate_provider: Callable[[int | None], str | None] | None = None,
    ):
        """Prefer verified popup OCR; use the Contacts detail panel only as fallback."""
        import json
        import os

        from wechat_ocr import DEFAULT_CONFIG, WeChatIdExtractor
        from wechat_ocr.extractor import label_diagnostics, reconcile_ocr_result

        frame = getattr(self, "last_stable_frame", None)
        right_panel_image, capture_latency = self.profile_image(
            page_verified=page_verified, uia_rect=uia_rect, image=frame
        )
        right_diag = label_diagnostics(self.last_profile_tokens)
        self.last_right_panel_diagnostics = right_diag
        popup_started = time.perf_counter()
        surface = None
        self.last_popup_status = PROFILE_POPUP_UNRESOLVED
        try:
            surface = self.open_friend_profile_visual(
                page_verified=page_verified,
                profile_crop=right_panel_image,
                profile_tokens=self.last_profile_tokens,
            )
        except NavigationFailure as exc:
            self.last_popup_status = exc.status
        popup_open_latency = time.perf_counter() - popup_started
        selected_image = surface.image if surface else right_panel_image
        ui_started = time.perf_counter()
        ui_candidate = (
            uia_candidate_provider(surface.hwnd if surface else None)
            if uia_candidate_provider
            else None
        )
        ui_latency = time.perf_counter() - ui_started

        benchmark = None
        if os.getenv("WECHAT_OCR_DIAGNOSTIC", "false").casefold() == "true":
            from wechat_ocr.extractor import diagnose_profile_rois

            benchmark = diagnose_profile_rois(
                {"popup" if surface else "right_panel": selected_image}
            )
            print(
                "WECHAT_OCR_DIAGNOSTIC "
                + json.dumps(benchmark, ensure_ascii=False, separators=(",", ":"))
            )
        started = time.perf_counter()
        ocr = WeChatIdExtractor(config=DEFAULT_CONFIG).extract(selected_image)
        ocr_latency = time.perf_counter() - started
        consensus_started = time.perf_counter()
        result = reconcile_ocr_result(ocr, ui_candidate)
        consensus_latency = time.perf_counter() - consensus_started
        popup_diagnostics = ocr.view_diagnostics[:2]
        self.last_popup_diagnostics = {
            "opened": surface is not None,
            "status": self.last_popup_status,
            "source": surface.source if surface else "none",
            "bounds": surface.bounds if surface else None,
            "confidence": surface.confidence if surface else None,
            "text_boxes": sum(
                int(item.get("detected_box_count", 0)) for item in popup_diagnostics
            )
            if surface
            else 0,
            "wechat_label_found": any(
                bool(item.get("has_wechat_label")) for item in popup_diagnostics
            )
            if surface
            else False,
            "candidate_found": any(ocr.ocr_candidates),
            "profile_verified": surface is not None,
            "uia_candidate_found": ui_candidate is not None,
            "benchmark": benchmark,
        }
        self.last_timing = {
            "contact_profile_capture": capture_latency,
            "popup_anchor": self.last_popup_timing.get(
                "popup_anchor", popup_open_latency
            ),
            "popup_open": self.last_popup_timing.get("popup_open", 0.0),
            "popup_wait": self.last_popup_timing.get("popup_stability_wait", 0.0),
            "uia": ui_latency,
            "ocr_dual_view": ocr_latency,
            "consensus": consensus_latency,
        }
        return result, capture_latency, ocr_latency


def image_fingerprint(image: Image.Image) -> str:
    """In-memory change evidence only; never writes an image artifact."""
    return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


class VisualContactSession:
    """A verified Contacts-page session for bounded, read-only OCR smoke."""

    visual_navigation = True

    def __init__(
        self,
        navigator: Win32VisualNavigation,
        page_evidence: PageEvidence,
        uia_window=None,
    ):
        self.navigator = navigator
        self.page_evidence = page_evidence
        self.uia_window = uia_window

    def _uia_candidate(self, popup_hwnd: int | None = None) -> str | None:
        try:
            from wechat_ocr import extract_wechat_id_from_uia

            window = self.uia_window
            if popup_hwnd:
                from pywinauto import Desktop

                window = Desktop(backend="uia").window(handle=popup_hwnd)
            if window is None:
                return None
            texts = [
                element.window_text()
                for element in window.descendants(control_type="Text")
            ]
            return extract_wechat_id_from_uia(texts)
        except Exception:
            return None

    def read_contacts(
        self, max_contacts: int = 5, start_index: int = 1
    ) -> list[dict[str, object]]:
        if not 1 <= max_contacts <= 5 or not 1 <= start_index <= max_contacts:
            raise ValueError("visual read-only smoke is limited to 1..5 contacts")
        row_started = time.perf_counter()
        rows = self.navigator.contact_rows(page_verified=self.page_evidence.confirmed)
        row_latency = time.perf_counter() - row_started
        if not rows:
            raise NavigationFailure(CONTACT_LIST_NOT_FOUND)
        results = []
        for contact_index in range(start_index, min(max_contacts, len(rows)) + 1):
            contact_started = time.perf_counter()
            self.navigator.select_contact(
                contact_index,
                page_verified=self.page_evidence.confirmed,
                smoke_limit=5,
            )
            result, capture_latency, ocr_latency = (
                self.navigator.extract_selected_profile(
                    page_verified=self.page_evidence.confirmed,
                    uia_candidate_provider=self._uia_candidate,
                )
            )
            popup = getattr(self.navigator, "last_popup_diagnostics", {})
            right = getattr(self.navigator, "last_right_panel_diagnostics", {})
            profile_status = result.status
            contact_total = (
                self.page_evidence.latency_seconds
                + row_latency
                + time.perf_counter()
                - contact_started
            )
            stage_timing = {
                **getattr(self.navigator, "last_contact_timing", {}),
                **getattr(self.navigator, "last_timing", {}),
                "total": contact_total,
            }
            stage_timing["contacts_page_verified"] = self.page_evidence.latency_seconds
            stage_timing["initial_contact_row_detect"] = row_latency
            accounted = sum(
                value for key, value in stage_timing.items() if key != "total"
            )
            results.append(
                {
                    "contact_index": contact_index,
                    "navigation_method": "visual",
                    "contacts_page_verified": self.page_evidence.confirmed,
                    "navigation_latency_seconds": self.page_evidence.latency_seconds,
                    "profile_found": True,
                    "profile_popup_opened": popup.get("opened", False),
                    "profile_popup_status": popup.get(
                        "status", "PROFILE_POPUP_UNRESOLVED"
                    ),
                    "surface_source": popup.get("source", "none"),
                    "popup_profile_verified": popup.get("profile_verified", False),
                    "微信号": result.value if result.is_confirmed else "无",
                    "微信号状态": profile_status,
                    "微信号来源": result.source,
                    "微信号置信度": result.confidence,
                    "微信号原因": result.reason or "",
                    "微信号候选": result.value if not result.is_confirmed else "",
                    "ocr_found": any(result.ocr_candidates),
                    "uia_found": result.ui_candidate is not None,
                    "agreement": "unavailable"
                    if result.ui_candidate is None
                    else "match"
                    if result.ui_candidate == result.value
                    else "mismatch",
                    "capture_latency_seconds": capture_latency,
                    "ocr_latency_seconds": ocr_latency,
                    "profile_stable": bool(
                        getattr(self.navigator, "last_profile_stability_seconds", None)
                        is not None
                    ),
                    "roi_strategy": "popup"
                    if popup.get("opened")
                    else "right_panel_secondary",
                    "detected_boxes": popup.get("text_boxes", 0),
                    "recognized_tokens": sum(
                        int(item.get("recognized_token_count", 0))
                        for item in result.view_diagnostics[:2]
                    ),
                    "wechat_label_found": popup.get("wechat_label_found", False)
                    if popup.get("opened")
                    else right.get("has_wechat_label", False),
                    "right_panel_text_boxes": right.get("detected_box_count", 0),
                    "right_panel_wechat_label_found": right.get(
                        "has_wechat_label", False
                    ),
                    "candidate_found": popup.get("candidate_found", False)
                    if popup.get("opened")
                    else any(result.ocr_candidates),
                    "candidate_masked": "<MASKED>" if result.value else "",
                    "failure_stage": result.reason or profile_status,
                    "stage_timing_seconds": stage_timing,
                    "unexplained_time_seconds": max(0.0, contact_total - accounted),
                    "total_latency_seconds": contact_total,
                }
            )
        return results
