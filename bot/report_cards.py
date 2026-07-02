from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

from bot.time_utils import format_datetime_dual_plain


ARABIC_RE = re.compile(r"[\u0600-\u06ff]")


@dataclass(slots=True, frozen=True)
class ProfileCardData:
    username: str
    full_name: str | None
    biography: str | None
    follower_count: int | None
    following_count: int | None
    post_count: int | None
    is_private: bool | None
    is_verified: bool


@dataclass(slots=True, frozen=True)
class AlertCardData:
    title: str
    username: str
    category: str
    primary_label: str
    primary_value: str
    secondary_label: str | None = None
    secondary_value: str | None = None
    accent: str = "gold"
    occurred_at: datetime | None = None


class ReportCardRenderer:
    WIDTH = 1080
    GOLD = (218, 177, 82)
    GOLD_LIGHT = (255, 225, 148)
    WHITE = (246, 241, 229)
    MUTED = (173, 166, 149)
    BACKGROUND = (5, 6, 8)

    @classmethod
    def render_profile(
        cls,
        profile: ProfileCardData,
        avatar_bytes: bytes | None = None,
    ) -> bytes:
        image = cls._background(1350)
        cls._glass_panel(image, (58, 48, 1022, 1300), 48)
        draw = ImageDraw.Draw(image)
        brand = cls._font(62, bold=True)
        caption = cls._font(25)
        username_font = cls._font(47, bold=True)
        name_font = cls._font(37, bold=True)
        metric_font = cls._font(43, bold=True)
        label_font = cls._font(24, bold=True)
        bio_font = cls._font(31)

        cls._text(draw, (100, 98), "FARSTAR WARNER", brand, cls.WHITE, anchor="la")
        cls._text(
            draw,
            (100, 166),
            "LIVE PROFILE INTELLIGENCE",
            caption,
            cls.MUTED,
            anchor="la",
        )
        cls._status_pill(draw, (776, 102, 950, 158), "LIVE", "green")

        avatar = cls._avatar(avatar_bytes, profile.username, 250, username_font)
        cls._gold_avatar_ring(image, avatar, (415, 220), 250)
        cls._text(
            draw,
            (540, 508),
            f"@{profile.username}",
            username_font,
            cls.WHITE,
            anchor="ma",
        )
        if profile.full_name:
            full_name = cls._ellipsize(draw, profile.full_name, name_font, 820)
            cls._text(
                draw,
                (540, 572),
                full_name,
                name_font,
                cls.GOLD_LIGHT,
                anchor="ma",
            )

        metrics = (
            ("FOLLOWERS", cls._number(profile.follower_count)),
            ("FOLLOWING", cls._number(profile.following_count)),
            ("POSTS", cls._number(profile.post_count)),
        )
        for index, (label, value) in enumerate(metrics):
            left = 92 + index * 303
            cls._mini_glass(image, (left, 655, left + 275, 820), 28)
            cls._text(
                draw,
                (left + 137, 700),
                value,
                metric_font,
                cls.WHITE,
                anchor="ma",
            )
            cls._text(
                draw,
                (left + 137, 770),
                label,
                label_font,
                cls.GOLD,
                anchor="ma",
            )

        cls._mini_glass(image, (92, 865, 988, 1168), 32)
        cls._text(draw, (130, 910), "BIOGRAPHY", label_font, cls.GOLD, anchor="la")
        biography = profile.biography or "No public biography is available."
        lines = cls._wrap(draw, cls._clip(biography, 300), bio_font, 805)
        y = 972
        for line in lines[:4]:
            rtl = bool(ARABIC_RE.search(line))
            cls._text(
                draw,
                (945 if rtl else 130, y),
                line,
                bio_font,
                cls.WHITE,
                anchor="ra" if rtl else "la",
            )
            y += 48

        visibility = "PRIVATE" if profile.is_private else "PUBLIC"
        verified = "VERIFIED" if profile.is_verified else "NOT VERIFIED"
        cls._status_pill(draw, (100, 1200, 290, 1258), visibility, "gold")
        cls._status_pill(draw, (310, 1200, 545, 1258), verified, "gold")
        cls._text(
            draw,
            (950, 1229),
            "SECURE MONITORING",
            caption,
            cls.MUTED,
            anchor="ra",
        )
        return cls._encode(image)

    @classmethod
    def render_alert(cls, alert: AlertCardData) -> bytes:
        image = cls._background(1080)
        cls._glass_panel(image, (58, 48, 1022, 1032), 48)
        draw = ImageDraw.Draw(image)
        brand = cls._font(56, bold=True)
        small = cls._font(24, bold=True)
        title_font = cls._font(45, bold=True)
        username_font = cls._font(42, bold=True)
        value_font = cls._font(66, bold=True)
        label_font = cls._font(27, bold=True)
        accent_rgb = cls._accent(alert.accent)

        cls._text(draw, (98, 100), "FARSTAR WARNER", brand, cls.WHITE, anchor="la")
        cls._text(
            draw,
            (98, 160),
            "SECURITY EVENT REPORT",
            small,
            cls.MUTED,
            anchor="la",
        )
        cls._status_pill(draw, (760, 102, 950, 158), alert.category, alert.accent)
        draw.ellipse((92, 232, 148, 288), fill=accent_rgb)
        draw.ellipse((108, 248, 132, 272), fill=cls.BACKGROUND)
        title = cls._ellipsize(draw, alert.title, title_font, 760)
        cls._text(
            draw,
            (950, 260),
            title,
            title_font,
            cls.WHITE,
            anchor="ra",
        )
        cls._text(
            draw,
            (950, 340),
            f"@{alert.username}",
            username_font,
            cls.GOLD_LIGHT,
            anchor="ra",
        )

        cls._mini_glass(image, (92, 415, 988, 685), 34)
        cls._text(
            draw,
            (950, 468),
            alert.primary_label,
            label_font,
            cls.MUTED,
            anchor="ra",
        )
        primary_value = cls._ellipsize(
            draw,
            alert.primary_value,
            value_font,
            800,
        )
        cls._text(
            draw,
            (950, 540),
            primary_value,
            value_font,
            accent_rgb,
            anchor="ra",
        )
        if alert.secondary_label and alert.secondary_value:
            secondary_text = cls._ellipsize(
                draw,
                f"{alert.secondary_label}: {alert.secondary_value}",
                label_font,
                800,
            )
            cls._text(
                draw,
                (950, 650),
                secondary_text,
                label_font,
                cls.WHITE,
                anchor="ra",
            )

        occurred_at = alert.occurred_at or datetime.now().astimezone()
        time_lines = format_datetime_dual_plain(occurred_at).split("; ")
        cls._mini_glass(image, (92, 735, 988, 902), 28)
        cls._text(draw, (130, 780), "EVENT TIME", small, cls.GOLD, anchor="la")
        for index, time_line in enumerate(time_lines[:2]):
            cls._text(
                draw,
                (950, 822 + index * 38),
                time_line,
                cls._font(23),
                cls.WHITE,
                anchor="ra",
            )
        cls._text(
            draw,
            (98, 970),
            "AUTOMATED EVIDENCE • VERIFIED PIPELINE",
            small,
            cls.MUTED,
            anchor="la",
        )
        return cls._encode(image)

    @classmethod
    def _background(cls, height: int) -> Image.Image:
        top = Image.new("RGBA", (cls.WIDTH, height), (14, 13, 10, 255))
        bottom = Image.new("RGBA", (cls.WIDTH, height), cls.BACKGROUND + (255,))
        gradient = Image.linear_gradient("L").resize((cls.WIDTH, height))
        image = Image.composite(bottom, top, gradient)
        light = Image.new("RGBA", image.size, (0, 0, 0, 0))
        light_draw = ImageDraw.Draw(light)
        light_draw.ellipse((-160, -180, 510, 480), fill=(224, 174, 65, 68))
        light_draw.ellipse((700, 530, 1240, 1170), fill=(255, 215, 128, 42))
        light_draw.ellipse(
            (260, height - 260, 760, height + 180), fill=(125, 90, 25, 38)
        )
        return Image.alpha_composite(
            image,
            light.filter(ImageFilter.GaussianBlur(120)),
        )

    @classmethod
    def _glass_panel(
        cls,
        image: Image.Image,
        box: tuple[int, int, int, int],
        radius: int,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=(16, 17, 19, 220),
            outline=cls.GOLD + (155,),
            width=2,
        )
        highlight = (box[0] + 20, box[1] + 18, box[2] - 20, box[1] + 22)
        draw.rounded_rectangle(highlight, radius=2, fill=(255, 234, 184, 70))
        image.alpha_composite(layer)

    @staticmethod
    def _mini_glass(
        image: Image.Image,
        box: tuple[int, int, int, int],
        radius: int,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=(255, 255, 255, 17),
            outline=(255, 220, 145, 64),
            width=2,
        )
        draw.line(
            (box[0] + radius, box[1] + 2, box[2] - radius, box[1] + 2),
            fill=(255, 248, 225, 55),
            width=2,
        )
        image.alpha_composite(layer)

    @classmethod
    def _status_pill(
        cls,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        text: str,
        accent: str,
    ) -> None:
        color = cls._accent(accent)
        draw.rounded_rectangle(
            box, radius=28, fill=(20, 20, 18), outline=color, width=2
        )
        cls._text(
            draw,
            ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2),
            text,
            cls._font(22, bold=True),
            color,
            anchor="mm",
        )

    @classmethod
    def _gold_avatar_ring(
        cls,
        image: Image.Image,
        avatar: Image.Image,
        position: tuple[int, int],
        size: int,
    ) -> None:
        ring = Image.new("RGBA", (size + 28, size + 28), (0, 0, 0, 0))
        ring_draw = ImageDraw.Draw(ring)
        ring_draw.ellipse((0, 0, size + 27, size + 27), fill=cls.GOLD_LIGHT + (255,))
        ring_draw.ellipse((8, 8, size + 19, size + 19), fill=(13, 14, 16, 255))
        image.alpha_composite(ring, (position[0] - 14, position[1] - 14))
        image.alpha_composite(avatar, position)

    @classmethod
    def _avatar(
        cls,
        avatar_bytes: bytes | None,
        username: str,
        size: int,
        font: ImageFont.FreeTypeFont,
    ) -> Image.Image:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        if avatar_bytes:
            try:
                avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                avatar = ImageOps.fit(
                    avatar,
                    (size, size),
                    method=Image.Resampling.LANCZOS,
                )
                avatar.putalpha(mask)
                return avatar
            except (OSError, ValueError):
                pass
        avatar = Image.new("RGBA", (size, size), (30, 27, 20, 255))
        avatar.putalpha(mask)
        draw = ImageDraw.Draw(avatar)
        cls._text(
            draw,
            (size // 2, size // 2),
            (username[:1] or "F").upper(),
            font,
            cls.GOLD_LIGHT,
            anchor="mm",
        )
        return avatar

    @staticmethod
    def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
        candidates = (
            "C:/Windows/Fonts/tahomabd.ttf" if bold else "C:/Windows/Fonts/tahoma.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        )
        layout = (
            ImageFont.Layout.RAQM
            if features.check_feature("raqm")
            else ImageFont.Layout.BASIC
        )
        for candidate in candidates:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size, layout_engine=layout)
        return ImageFont.load_default(size=size)

    @classmethod
    def _text(
        cls,
        draw: ImageDraw.ImageDraw,
        position: tuple[int, int],
        value: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple[int, int, int],
        *,
        anchor: str,
    ) -> None:
        if ARABIC_RE.search(value) and features.check_feature("raqm"):
            draw.text(
                position,
                value,
                font=font,
                fill=fill,
                anchor=anchor,
                direction="rtl",
                language="fa",
            )
            return
        rendered = cls._fallback_bidi(value)
        draw.text(position, rendered, font=font, fill=fill, anchor=anchor)

    @staticmethod
    def _fallback_bidi(value: str) -> str:
        if not ARABIC_RE.search(value):
            return value
        return get_display(arabic_reshaper.reshape(value), base_dir="R")

    @classmethod
    def _measure(
        cls,
        draw: ImageDraw.ImageDraw,
        value: str,
        font: ImageFont.FreeTypeFont,
    ) -> int:
        if ARABIC_RE.search(value) and features.check_feature("raqm"):
            box = draw.textbbox(
                (0, 0),
                value,
                font=font,
                direction="rtl",
                language="fa",
            )
        else:
            box = draw.textbbox((0, 0), cls._fallback_bidi(value), font=font)
        return box[2] - box[0]

    @classmethod
    def _ellipsize(
        cls,
        draw: ImageDraw.ImageDraw,
        value: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ) -> str:
        value = value.strip()
        if cls._measure(draw, value, font) <= max_width:
            return value
        shortened = value
        while shortened:
            shortened = shortened[:-1].rstrip()
            candidate = shortened + "..."
            if cls._measure(draw, candidate, font) <= max_width:
                return candidate
        return "..."

    @classmethod
    def _wrap(
        cls,
        draw: ImageDraw.ImageDraw,
        value: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ) -> list[str]:
        lines: list[str] = []
        for paragraph in value.splitlines() or [value]:
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if cls._measure(draw, candidate, font) <= max_width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        if len(lines) > 4:
            lines = lines[:4]
            lines[-1] = cls._ellipsize(draw, lines[-1] + "...", font, max_width)
        return lines

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        normalized = value.strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    @staticmethod
    def _number(value: int | None) -> str:
        if value is None:
            return "UNKNOWN"
        if value >= 1_000_000_000:
            rendered = f"{value / 1_000_000_000:.1f}B"
        elif value >= 1_000_000:
            rendered = f"{value / 1_000_000:.1f}M"
        elif value >= 10_000:
            rendered = f"{value / 1_000:.1f}K"
        else:
            rendered = f"{value:,}"
        return rendered.replace(".0", "")

    @classmethod
    def _accent(cls, name: str) -> tuple[int, int, int]:
        return {
            "green": (89, 222, 153),
            "red": (255, 105, 105),
            "blue": (105, 183, 255),
            "gold": cls.GOLD_LIGHT,
        }.get(name, cls.GOLD_LIGHT)

    @staticmethod
    def _encode(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=94, optimize=True)
        return output.getvalue()
