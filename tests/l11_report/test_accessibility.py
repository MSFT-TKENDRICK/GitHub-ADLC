"""L11 — the report has to be readable without seeing it.

The report's whole argument is that evidence is legible to a reviewer. A control
that changes state silently, or a comparison whose entire meaning is carried by
a visual blend, quietly excludes part of that audience -- and an evidence
artifact that only *some* reviewers can audit is not evidence, it is a picture.

Two rules are pinned here because both were shipped wrong once:

* **Navigation announces itself.** Moving between comparisons rewrites the slide
  label and the pairing rule in place. Without a live region a screen-reader user
  hears nothing at all and cannot tell that anything happened.
* **A composite is described as one thing.** The difference blend means nothing
  as two separately-announced images; it is exposed as a single labelled image
  that admits the view is visual-only and names the routes that are not.
"""

from __future__ import annotations

import re

from adlc.report.assets import JS
from adlc.report.html import _SHELL


def _tag(markup: str, element_id: str) -> str:
    """The opening tag carrying ``id="<element_id>"``."""
    match = re.search(rf"<[^<>]*\bid=\"{re.escape(element_id)}\"[^<>]*>", markup)
    assert match, f"no element with id={element_id!r} in the template"
    return match.group(0)


class TestSlideshowAnnouncesNavigation:
    """Prev/Next rewrite these in place, so they have to be live regions."""

    def test_the_slide_label_is_a_live_region(self) -> None:
        tag = _tag(_SHELL, "slide-label")
        assert 'aria-live="polite"' in tag, (
            "the slide label changes on every Prev/Next; without aria-live the "
            "change is silent to a screen reader"
        )

    def test_the_pairing_rule_is_a_live_region(self) -> None:
        """The rule is how a reader discounts a weak pairing -- it must be heard."""
        assert 'aria-live="polite"' in _tag(_SHELL, "slide-rule")

    def test_both_regions_are_polite_not_assertive(self) -> None:
        """Assertive would interrupt; navigation is not an emergency."""
        for element_id in ("slide-label", "slide-rule"):
            assert 'aria-live="assertive"' not in _tag(_SHELL, element_id)

    def test_the_navigation_buttons_are_labelled(self) -> None:
        for element_id in ("slide-prev", "slide-next"):
            assert "aria-label=" in _tag(_SHELL, element_id)

    def test_the_detail_panel_precedent_still_holds(self) -> None:
        """The task detail panel set this pattern; it must not regress either."""
        assert 'aria-live="polite"' in _tag(_SHELL, "node-detail")


class TestDifferenceBlendIsDescribed:
    """The blend is a composite; only the composite carries the meaning."""

    def test_the_blend_is_exposed_as_a_single_image(self) -> None:
        assert "box.setAttribute('role', 'img')" in JS

    def test_the_blend_carries_an_accessible_description(self) -> None:
        assert "box.setAttribute('aria-label'" in JS
        assert "Difference blend of " in JS

    def test_the_description_admits_the_view_is_visual_only(self) -> None:
        """Overstating what a blind reader gets here would be the worse failure."""
        assert "visual " in JS and "only" in JS

    def test_the_composited_layers_are_not_announced_separately(self) -> None:
        """Two images called "Before" and "After, blended" describe nothing."""
        assert "b.alt = 'Before'" not in JS
        assert "a.alt = 'After, blended'" not in JS
        assert "b.setAttribute('aria-hidden', 'true')" in JS
        assert "a.setAttribute('aria-hidden', 'true')" in JS

    def test_a_non_visual_route_to_the_same_comparison_is_named(self) -> None:
        assert "Side by side" in JS
        assert "Diff tab" in JS

    def test_the_side_by_side_figures_still_describe_their_own_images(self) -> None:
        """Only the blend is decorative; ordinary captures keep real alt text."""
        assert "img.alt = caption + ': ' + item.caption;" in JS
