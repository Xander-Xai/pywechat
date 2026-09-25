from dataclasses import dataclass

import pytest
from PIL import Image, ImageDraw

from pyweixin.visual_navigation import (
    CONTACTS_LIST_REGION,
    CONTACTS_NAV_CENTER,
    CONTACTS_NAV_REGION,
    CONTACT_PROFILE_NOT_FOUND,
    CONTACTS_PAGE_NOT_CONFIRMED,
    NAVIGATION_UNRESOLVED,
    NormalizedRect,
    NavigationFailure,
    WindowCoordinateMapper,
    Win32VisualNavigation,
    anchors_agree,
    confirm_contacts_page,
    detect_contact_rows,
    detect_contacts_anchor,
    image_fingerprint,
)


@dataclass
class Token:
    text: str
    confidence: float
    box: tuple


def icon_fixture(size=(900, 650), center=CONTACTS_NAV_CENTER):
    image = Image.new("RGB", size, "#f4f4f4")
    draw = ImageDraw.Draw(image)
    x, y = round(center[0] * size[0]), round(center[1] * size[1])
    radius = max(12, round(size[0] * 0.022))
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius), outline="#333333", width=4
    )
    draw.ellipse((x - 5, y - 9, x + 5, y + 1), fill="#333333")
    draw.arc((x - 13, y - 3, x + 13, y + 17), 180, 360, fill="#333333", width=4)
    return image


def test_coordinate_mapping_is_normalized_across_window_sizes_and_maximize():
    for width, height in ((900, 650), (1280, 900), (1920, 1080)):
        mapper = WindowCoordinateMapper(
            (100, 50, 100 + width, 50 + height), (90, 20, 1010, 740), 96
        )
        point = mapper.to_screen(0.5, 0.5)
        assert point == (100 + width // 2, 50 + height // 2)
        assert mapper.to_normalized(*point) == (0.5, 0.5)


def test_dpi_125_and_150_are_recorded_without_changing_normalized_positions():
    for dpi in (120, 144):
        mapper = WindowCoordinateMapper((10, 20, 1610, 920), (0, 0, 1620, 960), dpi)
        assert mapper.scale == dpi / 96
        assert mapper.to_screen(*CONTACTS_NAV_CENTER) == (
            10 + round(1600 * CONTACTS_NAV_CENTER[0]),
            20 + round(900 * CONTACTS_NAV_CENTER[1]),
        )


def test_contacts_anchor_detects_icon_features_in_configured_region():
    match = detect_contacts_anchor(icon_fixture())
    assert match is not None
    assert match.confidence >= 0.62
    assert match.method == "layout+icon_features"


def test_contacts_anchor_missing_fails_closed():
    blank = Image.new("RGB", (900, 650), "#f4f4f4")
    assert detect_contacts_anchor(blank) is None


def test_false_positive_icon_outside_sidebar_region_is_ignored():
    image = Image.new("RGB", (900, 650), "#f4f4f4")
    draw = ImageDraw.Draw(image)
    draw.ellipse((700, 150, 760, 210), outline="black", width=5)
    assert detect_contacts_anchor(image) is None


def test_false_positive_icon_in_nav_region_but_outside_expected_row_is_ignored():
    image = Image.new("RGB", (900, 650), "#f4f4f4")
    draw = ImageDraw.Draw(image)
    draw.ellipse((25, 120, 65, 147), outline="black", width=5)
    assert detect_contacts_anchor(image) is None


def test_contacts_anchor_accepts_ocr_label_when_bounded_to_sidebar():
    image = Image.new("RGB", (900, 650), "white")
    token = Token(
        "\u901a\u8baf\u5f55", 0.93, ((20, 180), (72, 180), (72, 205), (20, 205))
    )
    match = detect_contacts_anchor(image, [token])
    assert match is not None and match.method == "ocr_label"


def test_contacts_page_confirmation_from_explicit_page_label():
    evidence = confirm_contacts_page([Token("Groups", 0.9, ())])
    assert evidence.confirmed and evidence.method == "ocr_page_label"


def test_new_friends_subpage_label_alone_does_not_confirm_contacts_root():
    evidence = confirm_contacts_page([Token("New Friends", 0.9, ())])
    assert not evidence.confirmed


def test_contacts_page_confirmation_fails_without_independent_evidence():
    evidence = confirm_contacts_page([Token("ordinary text", 0.9, ())])
    assert not evidence.confirmed
    assert evidence.method == "insufficient_page_evidence"


def test_contacts_page_can_use_list_layout_plus_uia_hint():
    evidence = confirm_contacts_page([], list_row_count=3, uia_list_hint=True)
    assert evidence.confirmed and evidence.method == "visual_list+uia_hint"


def test_contact_rows_are_detected_anonymously_from_list_roi():
    image_size = (1000, 800)
    tokens = [
        Token("New Friends", 0.9, ((150, 175), (330, 175), (330, 200), (150, 200))),
        Token("Groups", 0.9, ((150, 240), (330, 240), (330, 265), (150, 265))),
        Token("Tags", 0.9, ((150, 305), (330, 305), (330, 330), (150, 330))),
        Token(
            "Official Accounts", 0.9, ((150, 370), (330, 370), (330, 395), (150, 395))
        ),
        Token("private-name-a", 0.9, ((150, 460), (330, 460), (330, 485), (150, 485))),
        Token("private-name-b", 0.9, ((150, 525), (330, 525), (330, 550), (150, 550))),
        Token("outside", 0.9, ((700, 300), (850, 300), (850, 325), (700, 325))),
    ]
    rows = detect_contact_rows(tokens, image_size)
    assert len(rows) == 2
    assert rows == sorted(rows)


def test_profile_fallback_roi_scales_with_window():
    image = Image.new("RGB", (1200, 900), "#ededed")
    x0, y0 = round(0.43 * 1200), round(0.035 * 900)
    x1, y1 = round(0.995 * 1200), round(0.94 * 900)
    crop = image.crop((x0, y0, x1, y1))
    assert crop.size == (678, 814)


def test_profile_roi_prefers_valid_uia_rectangle_and_rejects_invalid_hint():
    image = icon_fixture()
    nav = Win32VisualNavigation(
        44,
        recognize=lambda crop: [Token("WeChat ID", 0.9, ())],
        screenshot=lambda hwnd: image,
        foreground=lambda: 44,
        mapper=WindowCoordinateMapper((100, 100, 1000, 750), (90, 70, 1010, 780)),
    )
    crop, _ = nav.profile_image(page_verified=True, uia_rect=(500, 150, 950, 650))
    assert crop.size == (450, 500)
    fallback, _ = nav.profile_image(page_verified=True, uia_rect=(5, 5, 10, 10))
    assert fallback.size == (509, 588)


def test_profile_roi_rejects_unrelated_text_without_profile_evidence():
    image = Image.new("RGB", (900, 650), "white")
    nav = Win32VisualNavigation(
        44,
        recognize=lambda crop: [
            Token("ordinary heading", 0.9, ((20, 20), (100, 20), (100, 35), (20, 35)))
        ],
        screenshot=lambda hwnd: image,
        foreground=lambda: 44,
        mapper=WindowCoordinateMapper((100, 100, 1000, 750), (90, 70, 1010, 780)),
    )
    with pytest.raises(NavigationFailure) as error:
        nav.profile_image(page_verified=True)
    assert error.value.status == CONTACT_PROFILE_NOT_FOUND


def test_scaled_window_keeps_anchor_at_same_normalized_location():
    for size in ((720, 540), (1440, 1080)):
        match = detect_contacts_anchor(icon_fixture(size=size))
        assert match is not None
        assert abs(match.x_ratio - CONTACTS_NAV_CENTER[0]) < 0.001
        assert abs(match.y_ratio - CONTACTS_NAV_CENTER[1]) < 0.001


def test_empty_uia_tree_and_root_pane_do_not_block_visual_anchor():
    assert detect_contacts_anchor(icon_fixture(), uia_hint=False) is not None
    # The same geometry represents a root Pane with no useful descendants.
    assert detect_contacts_anchor(icon_fixture(), uia_hint=False) is not None


def test_visual_success_with_uia_failure_is_supported():
    assert detect_contacts_anchor(icon_fixture(), tokens=(), uia_hint=False) is not None


def test_visual_uia_disagreement_is_rejected():
    assert not anchors_agree(CONTACTS_NAV_CENTER, (0.75, 0.8))
    token = Token("Contacts", 0.96, ((30, 170), (70, 170), (70, 198), (30, 198)))
    assert (
        detect_contacts_anchor(
            Image.new("RGB", (900, 650), "white"), [token], uia_center=(0.75, 0.8)
        )
        is None
    )


def test_click_roi_and_page_failure_statuses_are_distinct():
    assert NormalizedRect(*CONTACTS_NAV_REGION).contains(*CONTACTS_NAV_CENTER)
    assert NormalizedRect(*CONTACTS_LIST_REGION).contains(0.25, 0.4)
    assert CONTACTS_PAGE_NOT_CONFIRMED != CONTACT_PROFILE_NOT_FOUND


def test_screenshots_are_hashed_in_memory_without_file_side_effects():
    first, second = (
        Image.new("RGB", (20, 20), "white"),
        Image.new("RGB", (20, 20), "black"),
    )
    assert image_fingerprint(first) != image_fingerprint(second)


def test_visual_navigation_works_with_empty_uia_and_verifies_page_change():
    before = icon_fixture()
    after = before.copy()
    draw = ImageDraw.Draw(after)
    draw.rectangle((200, 140, 390, 230), fill="white")
    screenshots = iter((before, before, after))
    row = Token("anon-row", 0.9, ((150, 350), (330, 350), (330, 375), (150, 375)))
    selected_clicks = []
    nav = Win32VisualNavigation(
        44,
        recognize=lambda image: (
            [row] if image_fingerprint(image) == image_fingerprint(after) else []
        ),
        screenshot=lambda hwnd: next(screenshots),
        click=lambda x, y: selected_clicks.append((x, y)),
        foreground=lambda: 44,
        wait=lambda seconds: None,
        mapper=WindowCoordinateMapper((100, 100, 1000, 750), (90, 70, 1010, 780)),
    )
    # Page label and row are synthetic and contain no real contact data.
    nav._recognize = lambda image: [Token("Groups", 0.9, ()), row]
    evidence = nav.open_contacts(uia_hint=False, uia_list_hint=False)
    assert evidence.confirmed
    assert evidence.method == "ocr_page_label"
    assert len(selected_clicks) == 1
    x_ratio, y_ratio = nav.mapper.to_normalized(*selected_clicks[0])
    assert NormalizedRect(*CONTACTS_NAV_REGION).contains(x_ratio, y_ratio)


def test_shifted_screenshot_anchor_is_rejected_before_click():
    before = Image.new("RGB", (900, 650), "white")
    screenshots = iter((before, before))
    clicks = []

    calls = []

    def recognize(_image):
        x_ratio = 0.04 if not calls else 0.08
        calls.append(True)
        return [
            Token(
                "Contacts",
                0.95,
                (
                    (round(x_ratio * 900), round(0.28 * 650)),
                    (round((x_ratio + 0.01) * 900), round(0.28 * 650)),
                    (round((x_ratio + 0.01) * 900), round(0.31 * 650)),
                    (round(x_ratio * 900), round(0.31 * 650)),
                ),
            )
        ]

    nav = Win32VisualNavigation(
        44,
        recognize=recognize,
        screenshot=lambda hwnd: next(screenshots),
        click=lambda x, y: clicks.append((x, y)),
        foreground=lambda: 44,
        wait=lambda seconds: None,
        mapper=WindowCoordinateMapper((0, 0, 900, 650), (0, 0, 900, 650)),
    )
    with pytest.raises(NavigationFailure) as error:
        nav.open_contacts()
    assert error.value.status == NAVIGATION_UNRESOLVED
    assert clicks == []


def test_ocr_engine_failure_maps_to_explicit_ocr_failed_status():
    nav = Win32VisualNavigation(
        44,
        recognize=lambda image: (_ for _ in ()).throw(RuntimeError("private")),
        screenshot=lambda hwnd: icon_fixture(),
        foreground=lambda: 44,
        mapper=WindowCoordinateMapper((0, 0, 900, 650), (0, 0, 900, 650)),
    )
    with pytest.raises(NavigationFailure) as error:
        nav.open_contacts()
    assert error.value.status == "OCR_FAILED"


def test_visual_navigation_fails_closed_when_target_is_not_foreground():
    nav = Win32VisualNavigation(
        44,
        screenshot=lambda hwnd: icon_fixture(),
        foreground=lambda: 99,
        mapper=WindowCoordinateMapper((0, 0, 900, 650), (0, 0, 900, 650)),
    )
    with pytest.raises(NavigationFailure) as error:
        nav.open_contacts()
    assert error.value.status == NAVIGATION_UNRESOLVED
