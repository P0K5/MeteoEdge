"""Tests for dashboard logo asset optimization (issue #806).

Verifies that:
1. Logo file is optimized to <10KB
2. Logo dimensions are appropriate (96x96px)
3. HTML img tag includes width/height attributes to prevent layout shift
"""
import os
from pathlib import Path

import pytest


class TestDashboardLogoOptimization:
    """Tests for logo asset optimization and markup correctness."""

    def test_logo_file_exists(self):
        """Logo PNG file must exist at src/dashboard/static/logo.png."""
        logo_path = Path("src/dashboard/static/logo.png")
        assert logo_path.exists(), f"Logo not found at {logo_path}"

    def test_logo_file_size_under_10kb(self):
        """Logo must be under 10KB after optimization (issue #806 requirement)."""
        logo_path = Path("src/dashboard/static/logo.png")
        size_bytes = logo_path.stat().st_size
        size_kb = size_bytes / 1024
        assert size_kb < 10, f"Logo is {size_kb:.1f}KB, must be <10KB"

    def test_logo_dimensions_96x96(self):
        """Logo dimensions must be 96x96 pixels.

        Resizing from 1254x1254 to 96x96 provides the best balance between
        quality and file size for a dashboard logo.
        """
        from PIL import Image
        logo_path = Path("src/dashboard/static/logo.png")
        img = Image.open(logo_path)
        assert img.size == (96, 96), f"Logo dimensions are {img.size}, expected (96, 96)"

    def test_logo_format_is_png(self):
        """Logo must be in PNG format."""
        from PIL import Image
        logo_path = Path("src/dashboard/static/logo.png")
        img = Image.open(logo_path)
        assert img.format == "PNG", f"Logo format is {img.format}, expected PNG"

    def test_html_logo_img_tag_has_width_attribute(self):
        """The logo <img> tag must have an explicit width attribute to prevent layout shift."""
        html_path = Path("src/dashboard/static/index.html")
        content = html_path.read_text(encoding="utf-8")
        # Look for the logo img tag with width attribute
        assert 'src="/logo.png"' in content
        assert 'width="96"' in content, "Logo img tag missing width='96' attribute"

    def test_html_logo_img_tag_has_height_attribute(self):
        """The logo <img> tag must have an explicit height attribute to prevent layout shift."""
        html_path = Path("src/dashboard/static/index.html")
        content = html_path.read_text(encoding="utf-8")
        # Look for the logo img tag with height attribute
        assert 'src="/logo.png"' in content
        assert 'height="96"' in content, "Logo img tag missing height='96' attribute"

    def test_html_logo_img_tag_attributes_together(self):
        """The logo <img> tag must have both width and height attributes in the same tag."""
        html_path = Path("src/dashboard/static/index.html")
        content = html_path.read_text(encoding="utf-8")
        # Find the img tag line containing logo
        import re
        logo_img_pattern = r'<img\s+[^>]*src="/logo\.png"[^>]*>'
        match = re.search(logo_img_pattern, content)
        assert match, "Logo img tag not found"
        img_tag = match.group(0)
        assert 'width="96"' in img_tag, "width attribute not in logo img tag"
        assert 'height="96"' in img_tag, "height attribute not in logo img tag"

    def test_html_logo_img_tag_has_alt_text(self):
        """The logo <img> tag must have an alt attribute for accessibility."""
        html_path = Path("src/dashboard/static/index.html")
        content = html_path.read_text(encoding="utf-8")
        import re
        logo_img_pattern = r'<img\s+[^>]*src="/logo\.png"[^>]*>'
        match = re.search(logo_img_pattern, content)
        assert match, "Logo img tag not found"
        img_tag = match.group(0)
        assert 'alt=' in img_tag, "alt attribute missing from logo img tag"

    def test_logo_color_mode_is_rgb(self):
        """Logo should be in RGB color mode for efficient compression."""
        from PIL import Image
        logo_path = Path("src/dashboard/static/logo.png")
        img = Image.open(logo_path)
        # RGB or RGBA are acceptable; RGBA is actually fine and adds transparency support
        assert img.mode in ("RGB", "RGBA"), f"Logo color mode is {img.mode}, expected RGB or RGBA"
