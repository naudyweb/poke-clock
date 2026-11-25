"""
All 5 Pokémon Game Boy / Game Boy Color (Red/Blue/Yellow & Gold/Silver/Crystal)
240x240 screens with authentic Gen-1/Gen-2 battle-HUD craftsmanship, crisp pixel
hierarchy, zero label collisions, and PC-only animated Pokémon + Spotify EQ waves:
  1. gemini   — Battle HUD (Antigravity Gemini Token Usage)
  2. clock    — PokéGear Clock (Day / Night + Live Temp)
  3. weather  — Weather Battle HUD + 3-Day Forecast
  4. spotify  — Concert Stage (Spotify Now Playing, exclusive auto)
  5. notify   — Gen-1/2 Encounter Alert (CLI / API)
"""

from __future__ import annotations

from datetime import datetime

from pixel_art import (
    BULBASAUR_PALETTE,
    BULBASAUR_SPRITE,
    CHARMANDER_PALETTE,
    CHARMANDER_SPRITE,
    GENGAR_PALETTE,
    GENGAR_SPRITE,
    JIGGLYPUFF_PALETTE,
    JIGGLYPUFF_SPRITE,
    MINI_CLOUD,
    MINI_CLOUD_PAL,
    MINI_MOON,
    MINI_MOON_PAL,
    MINI_RAIN,
    MINI_RAIN_PAL,
    MINI_SUN,
    MINI_SUN_PAL,
    NOTE_PALETTE,
    NOTE_SPRITE,
    PIKACHU_PALETTE,
    PIKACHU_SPRITE,
    POKEBALL_PALETTE,
    POKEBALL_SPRITE,
    SNORLAX_PALETTE,
    SNORLAX_SPRITE,
    SPARKLE_PALETTE,
    SPARKLE_SPRITE,
    SQUIRTLE_PALETTE,
    SQUIRTLE_SPRITE,
    pixel_text_width,
    render_battle_bracket,
    render_battle_pad,
    render_dotted_rule,
    render_horizon_dither,
    render_pixel_text,
    render_signal_bars,
    render_sprite,
    sanitize_pixel_text,
    truncate_pixel_text,
    wrap_pixel_text,
)

# All PC-side animated elements live strictly inside y = 0..95 (6x16 JPEG MCU boundary)
ANIM_STRIP_H = 96

DAYS_SHORT_ES = ["LUN", "MAR", "MIE", "JUE", "VIE", "SAB", "DOM"]
MONTHS_SHORT_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]

SPRITE_CATALOG: dict[str, tuple[list[str], dict[str, str], str]] = {
    "pikachu": (PIKACHU_SPRITE, PIKACHU_PALETTE, "#F8B800"),
    "charmander": (CHARMANDER_SPRITE, CHARMANDER_PALETTE, "#F87828"),
    "squirtle": (SQUIRTLE_SPRITE, SQUIRTLE_PALETTE, "#3898F8"),
    "bulbasaur": (BULBASAUR_SPRITE, BULBASAUR_PALETTE, "#30A848"),
    "gengar": (GENGAR_SPRITE, GENGAR_PALETTE, "#7858A8"),
    "snorlax": (SNORLAX_SPRITE, SNORLAX_PALETTE, "#387888"),
    "jigglypuff": (JIGGLYPUFF_SPRITE, JIGGLYPUFF_PALETTE, "#E078A0"),
}

OFFLINE_POKEMON_ORDER: tuple[str, ...] = (
    "pikachu",
    "charmander",
    "squirtle",
    "bulbasaur",
    "gengar",
    "snorlax",
    "jigglypuff",
)

# Authentic GBC Crystal idle trajectories: (dx, dy, shadow_shrink)
# Every pose keeps sprite inside y = 8..88 (cleanly above y=96 partial-blit seam)
_POKEMON_MOTION: dict[str, list[tuple[int, int, int]]] = {
    "pikachu":    [(0, 0, 0), (0, -6, 6), (2, -2, 2), (-2, -4, 4)],
    "jigglypuff": [(0, 0, 0), (2, -6, 6), (0, -2, 2), (-2, -6, 6)],
    "gengar":     [(0, -2, 2), (0, -6, 6), (2, -8, 7), (-2, -4, 4)],
    "charmander": [(0, 0, 0), (0, -4, 4), (0, -6, 6), (0, -2, 2)],
    "squirtle":   [(0, 0, 0), (0, -4, 4), (2, -4, 4), (0, -2, 2)],
    "bulbasaur":  [(0, 0, 0), (0, -4, 4), (0, -2, 2), (0, -4, 4)],
    "snorlax":    [(0, 0, 0), (0, -2, 2), (0, -2, 2), (0, 0, 0)],
}

# 6-frame smooth traveling Spotify equalizer wave (10 bars, heights 6..26px)
_SPOTIFY_EQ_FRAMES: list[list[int]] = [
    [8,  16, 22, 14, 24, 18, 12, 20, 14, 8],
    [14, 22, 16, 24, 18, 12, 22, 16, 10, 14],
    [20, 14, 10, 18, 26, 20, 16, 10, 18, 20],
    [12, 8,  18, 24, 16, 26, 20, 14, 22, 12],
    [16, 20, 26, 16, 10, 18, 24, 18, 12, 16],
    [10, 18, 14, 10, 20, 24, 14, 22, 16, 10],
]
_SPOTIFY_BGM_WIDTHS = [80, 90, 96, 84, 92, 76]

PALETTES: dict[str, dict[str, str]] = {
    "gbc_classic": {
        "bg": "#F8F8F0",
        "card": "#FFFFFF",
        "ink": "#181820",
        "border": "#181820",
        "muted": "#5E6670",
        "shadow": "#CCD4C4",
        "divider": "#C8D2C0",
        "blue": "#205CC0",
        "green": "#15803D",
        "top_band": "#D8E8C8",
        "pad_top": "#90B870",
        "pad_mid": "#C4E2A4",
        "pad_bot": "#6E9650",
        "bar_empty": "#D4DCCE",
        "dither": "#E2EAD8",
    },
    "gbc_dark": {
        "bg": "#101524",
        "card": "#171F36",
        "ink": "#F2F5FF",
        "border": "#7A8EB8",
        "muted": "#92A2C4",
        "shadow": "#070B14",
        "divider": "#2C3B5E",
        "blue": "#68ACFF",
        "green": "#34D399",
        "top_band": "#243152",
        "pad_top": "#2A3B5E",
        "pad_mid": "#3B5180",
        "pad_bot": "#1C2742",
        "bar_empty": "#2A3652",
        "dither": "#1A233A",
    },
    "gb_green": {
        "bg": "#9BBC0F",
        "card": "#8BAC0F",
        "ink": "#0F380F",
        "border": "#0F380F",
        "muted": "#306230",
        "shadow": "#306230",
        "divider": "#306230",
        "blue": "#0F380F",
        "green": "#0F380F",
        "top_band": "#8BAC0F",
        "pad_top": "#306230",
        "pad_mid": "#8BAC0F",
        "pad_bot": "#0F380F",
        "bar_empty": "#306230",
        "dither": "#8BAC0F",
    },
    "pokedex_red": {
        "bg": "#FFF5F2",
        "card": "#FFFFFF",
        "ink": "#1C1418",
        "border": "#1C1418",
        "muted": "#72545C",
        "shadow": "#E4C0B8",
        "divider": "#E8CCC4",
        "blue": "#C82828",
        "green": "#15803D",
        "top_band": "#DC3238",
        "pad_top": "#D49088",
        "pad_mid": "#F2C4BC",
        "pad_bot": "#AC645C",
        "bar_empty": "#E8D2CA",
        "dither": "#F5DFD8",
    },
}

OFFLINE_PALETTE_ORDER: tuple[str, ...] = (
    "gbc_classic",
    "gbc_dark",
    "gb_green",
    "pokedex_red",
)


def resolve_palette(cfg: dict | None, now: datetime | None = None) -> dict[str, str]:
    cfg = cfg or {}
    name = str(cfg.get("palette") or "gbc_classic")
    if cfg.get("auto_night_palette") and now is not None:
        ns = int(cfg.get("night_start", 23))
        ne = int(cfg.get("night_end", 7))
        h = now.hour
        in_night = (h >= ns or h < ne) if ns > ne else (ns <= h < ne)
        if in_night:
            name = "gbc_dark"
    return PALETTES.get(name, PALETTES["gbc_classic"])


def resolve_palette_bg(cfg: dict | None = None, now: datetime | None = None) -> str:
    return resolve_palette(cfg, now)["bg"]


def _get_sprite(name: str, fallback: str = "pikachu") -> tuple[str, list[str], dict[str, str], str]:
    key = (name or "").lower()
    if key not in SPRITE_CATALOG:
        key = fallback
    grid, pal, accent = SPRITE_CATALOG[key]
    return key, grid, pal, accent


def _pokemon_motion(sprite_key: str, anim_frame: int) -> tuple[int, int, int]:
    seq = _POKEMON_MOTION.get(sprite_key, _POKEMON_MOTION["pikachu"])
    return seq[anim_frame % len(seq)]


def _render_twinkle(x: int, y: int, color: str, alt: bool = False) -> str:
    if alt:
        return (
            f'<rect x="{x + 2}" y="{y}" width="2" height="6" fill="{color}"/>'
            f'<rect x="{x}" y="{y + 2}" width="6" height="2" fill="{color}"/>'
        )
    return f'<rect x="{x + 2}" y="{y + 2}" width="2" height="2" fill="{color}"/>'


def _render_defeated_fx(sx: int, sy: int, anim_frame: int) -> str:
    """Render defeated Pokémon FX: X eyes, spinning stars — like fainting in battle."""
    f4 = anim_frame % 4
    parts: list[str] = []

    # X eyes: Charmander eye is at grid (4,3), scale=4 → offset from sprite origin
    # The eye white pixel is at grid col 4, row 3. With scale=4, that's 4x4 pixels.
    # Draw a larger, more visible X eye (8x8 pixels) centered on the eye position
    x_color = "#E83838"

    # Single visible eye (Charmander faces right, one eye visible)
    # Grid (4,3) → pixel offset: 4*4=16, 3*4=12 from sprite origin
    # Center the 8x8 X at the eye center: sx+18, sy+14
    ex, ey = sx + 16, sy + 12
    # Draw 8x8 X pattern
    parts.append(
        f'<rect x="{ex}" y="{ey}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 2}" y="{ey}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 4}" y="{ey}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 6}" y="{ey}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 1}" y="{ey + 1}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 5}" y="{ey + 1}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 2}" y="{ey + 2}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 4}" y="{ey + 2}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 3}" y="{ey + 3}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 2}" y="{ey + 4}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 4}" y="{ey + 4}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 1}" y="{ey + 5}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 5}" y="{ey + 5}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex}" y="{ey + 6}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 2}" y="{ey + 6}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 4}" y="{ey + 6}" width="2" height="2" fill="{x_color}"/>'
        f'<rect x="{ex + 6}" y="{ey + 6}" width="2" height="2" fill="{x_color}"/>'
    )

    # Spinning stars around the head (like in Pokémon faint animation)
    star_positions = [
        (sx + 44, sy - 4),   # top right
        (sx + 12, sy - 8),   # top left
        (sx + 56, sy + 4),   # right
    ]
    for idx, (stx, sty) in enumerate(star_positions):
        # Alternate visibility per frame for spinning effect
        if (f4 + idx) % 2 == 0:
            star_y = sty - 2
        else:
            star_y = sty
        parts.append(
            f'<rect x="{stx + 2}" y="{star_y}" width="2" height="6" fill="#F8E030"/>'
            f'<rect x="{stx}" y="{star_y + 2}" width="6" height="2" fill="#F8E030"/>'
        )

    return "".join(parts)


def _render_species_fx(sprite_key: str, sx: int, sy: int, anim_frame: int) -> str:
    """Authentic Gen-2 expressive micro-FX per Pokémon species (all strictly inside y=8..92)."""
    f4 = anim_frame % 4
    if sprite_key == "pikachu":
        # Tiny electric cheek sparks alternating left/right
        if f4 == 1:
            return (
                f'<rect x="{sx + 2}" y="{sy + 30}" width="4" height="2" fill="#F8E030"/>'
                f'<rect x="{sx}" y="{sy + 32}" width="2" height="2" fill="#FFFFFF"/>'
            )
        if f4 == 3:
            return (
                f'<rect x="{sx + 66}" y="{sy + 26}" width="4" height="2" fill="#F8E030"/>'
                f'<rect x="{sx + 68}" y="{sy + 24}" width="2" height="2" fill="#FFFFFF"/>'
            )
        return ""
    if sprite_key == "charmander":
        # Flickering tail flame tip
        fy = sy + (6 if f4 in (1, 2) else 8)
        col = "#FFF078" if f4 % 2 else "#F87828"
        return (
            f'<rect x="{sx + 58}" y="{fy}" width="4" height="4" fill="{col}"/>'
            f'<rect x="{sx + 60}" y="{fy - 2}" width="2" height="2" fill="#F03020"/>'
        )
    if sprite_key == "snorlax":
        # Cozy floating pixel Z rising gently while napping
        zy = max(10, sy - 2 - f4 * 2)
        zx = sx + 56 + (2 if f4 >= 2 else 0)
        return (
            f'<rect x="{zx}" y="{zy}" width="6" height="2" fill="#5890A8"/>'
            f'<rect x="{zx + 2}" y="{zy + 2}" width="2" height="2" fill="#5890A8"/>'
            f'<rect x="{zx}" y="{zy + 4}" width="6" height="2" fill="#5890A8"/>'
        )
    if sprite_key == "gengar":
        # Spooky violet wisps orbiting subtly
        wy = sy + 12 - (f4 % 2) * 4
        return (
            f'<rect x="{sx - 4}" y="{wy}" width="4" height="4" fill="#9878D8"/>'
            f'<rect x="{sx + 68}" y="{wy + 8}" width="4" height="4" fill="#B898F8"/>'
        )
    if sprite_key == "squirtle":
        # Cheerful water glint droplets
        if f4 in (1, 2):
            return (
                f'<rect x="{sx + 64}" y="{sy + 14 - f4 * 2}" width="2" height="4" fill="#68C8FF"/>'
                f'<rect x="{sx + 4}" y="{sy + 12}" width="2" height="2" fill="#FFFFFF"/>'
            )
        return ""
    if sprite_key == "bulbasaur":
        # Fresh pixel leaf glint above bulb
        if f4 in (1, 3):
            return _render_twinkle(sx + 54, max(8, sy - 2), "#48B848", alt=(f4 == 1))
        return ""
    if sprite_key == "jigglypuff":
        # Tiny musical sparkle pop
        if f4 in (1, 3):
            return _render_twinkle(sx - 2, sy + 6, "#F8A8C8", alt=(f4 == 1))
        return ""
    return ""


def _render_dialogue_box_themed(x: int, y: int, w: int, h: int, pal: dict[str, str], cursor: bool = True) -> str:
    """Authentic Pokémon Gold/Silver/Crystal double-border notched dialogue box."""
    b = pal["border"]
    bg = pal["bg"]
    parts = [
        f'<rect x="{x + 2}" y="{y}" width="{w - 4}" height="{h}" fill="{b}"/>',
        f'<rect x="{x}" y="{y + 2}" width="{w}" height="{h - 4}" fill="{b}"/>',
        f'<rect x="{x + 4}" y="{y + 4}" width="{w - 8}" height="{h - 8}" fill="{bg}"/>',
        f'<rect x="{x + 6}" y="{y + 6}" width="{w - 12}" height="{h - 12}" fill="{b}"/>',
        f'<rect x="{x + 8}" y="{y + 8}" width="{w - 16}" height="{h - 16}" fill="{bg}"/>',
        # Iconic 4 corner pixel ornaments
        f'<rect x="{x + 2}" y="{y + 2}" width="6" height="6" fill="{b}"/>',
        f'<rect x="{x + 4}" y="{y + 4}" width="2" height="2" fill="{bg}"/>',
        f'<rect x="{x + w - 8}" y="{y + 2}" width="6" height="6" fill="{b}"/>',
        f'<rect x="{x + w - 6}" y="{y + 4}" width="2" height="2" fill="{bg}"/>',
        f'<rect x="{x + 2}" y="{y + h - 8}" width="6" height="6" fill="{b}"/>',
        f'<rect x="{x + 4}" y="{y + h - 6}" width="2" height="2" fill="{bg}"/>',
        f'<rect x="{x + w - 8}" y="{y + h - 8}" width="6" height="6" fill="{b}"/>',
        f'<rect x="{x + w - 6}" y="{y + h - 6}" width="2" height="2" fill="{bg}"/>',
    ]
    if cursor:
        cx = x + w - 22
        cy = y + h - 18
        parts.extend([
            f'<rect x="{cx}" y="{cy}" width="10" height="2" fill="#E83838"/>',
            f'<rect x="{cx + 2}" y="{cy + 2}" width="6" height="2" fill="#E83838"/>',
            f'<rect x="{cx + 4}" y="{cy + 4}" width="2" height="2" fill="#E83838"/>',
        ])
    return "".join(parts)


def _render_card_frame(x: int, y: int, w: int, h: int, pal: dict[str, str], top_accent: str | None = None) -> str:
    """Clean GBC card container with tight 2px pixel drop-shadow."""
    parts = [
        f'<rect x="{x + 2}" y="{y + 2}" width="{w}" height="{h}" fill="{pal["shadow"]}"/>',
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{pal["border"]}"/>',
        f'<rect x="{x + 2}" y="{y + 2}" width="{w - 4}" height="{h - 4}" fill="{pal["card"]}"/>',
    ]
    if top_accent:
        parts.append(f'<rect x="{x + 2}" y="{y + 2}" width="{w - 4}" height="4" fill="{top_accent}"/>')
    return "".join(parts)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 100_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 100_000:
        return f"{n / 1_000:.0f}K"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(n)


# ============================================================
# 1. GEMINI TOKENS (Gen-2 Battle HUD)
# ============================================================
def build_gemini_svg(stats: dict, cfg: dict | None = None, anim_frame: int = 0, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now().astimezone()
    pal = resolve_palette(cfg, now)
    sprites = (cfg or {}).get("sprites") or {}
    sp_key, sp_grid, sp_pal, _ = _get_sprite(sprites.get("gemini", "pikachu"), "pikachu")

    out_of_tokens = stats.get("out_of_tokens", False)

    if out_of_tokens:
        # Defeated: Pokémon falls to the ground, no shake
        dx, dy, sh = 0, 20, 4  # dropped down by 20px, larger shadow
    else:
        dx, dy, sh = _pokemon_motion(sp_key, anim_frame)

    today_total_str = fmt_tokens(stats["today_total"])
    week_total_str = fmt_tokens(stats["week_total"])
    updated_hm = stats["updated_hm"]
    daily_reset = stats.get("daily_reset", "--:--")
    weekly_reset = stats.get("weekly_reset", "--:--")

    if out_of_tokens:
        # Replace IN/OUT with reset counters when out of tokens
        in_str = daily_reset
        out_str = weekly_reset
        cache_str = "--"
    else:
        in_str = fmt_tokens(stats["today_in"])
        out_str = fmt_tokens(stats["today_out"])
        cache_str = fmt_tokens(stats["today_cache"])
    steps = stats["today_steps"]
    lv_val = min(99, max(1, steps)) if steps > 0 else 1
    cache_pct = stats["cache_pct"]

    if cache_pct >= 50:
        hp_color, hp_hi = "#30B848", "#78E878"
    elif cache_pct >= 20:
        hp_color, hp_hi = "#F8C828", "#FFF080"
    else:
        hp_color, hp_hi = "#E83838", "#F88888"

    hp_max_w = 60
    hp_fill_w = (max(2, int(round((cache_pct / 100.0) * hp_max_w))) // 2) * 2 if cache_pct > 0 else 0
    hero_scale = 4 if pixel_text_width(today_total_str, 4) <= 122 else 3

    # Build bars or "SIN TOKENS" message
    if out_of_tokens:
        # Flash the message every 2 frames
        f4 = anim_frame % 4
        show_msg = f4 < 3  # visible 3 out of 4 frames (blink effect)
        if show_msg:
            bar_parts_svg = render_pixel_text("SIN TOKENS", 120, 130, 3, "#E83838", align="center", shadow=pal['shadow'], shadow_offset=2)
            bar_parts_svg += render_pixel_text("ESPERA MAÑANA", 120, 152, 2, pal['muted'], align="center")
        else:
            bar_parts_svg = render_pixel_text("SIN TOKENS", 120, 130, 3, pal['muted'], align="center")
            bar_parts_svg += render_pixel_text("ESPERA MAÑANA", 120, 152, 2, pal['bar_empty'], align="center")
    else:
        max_day = max((d["total"] for d in stats["week_days"]), default=0)
        bar_parts: list[str] = []
        start_x, slot_w, bar_w, base_y, max_h = 20, 29, 18, 148, 24

        for idx, day in enumerate(stats["week_days"]):
            bx = start_x + idx * slot_w
            cx = bx + bar_w // 2
            val = day["total"]
            if max_day > 0 and val > 0:
                h = (max(6, int(round((val / max_day) * max_h))) // 2) * 2
            else:
                h = 2
            by = base_y - h
            if day["is_today"]:
                fill, hi, border, lbl_col = "#F8B800", "#FFF090", pal["border"], pal["ink"]
            elif val > 0:
                fill, hi, border, lbl_col = "#3898F8", "#88D0FF", pal["border"], pal["muted"]
            else:
                fill, hi, border, lbl_col = pal["bar_empty"], pal["bar_empty"], pal["muted"], pal["muted"]

            if h >= 6:
                bar_parts.append(
                    f'<rect x="{bx}" y="{by}" width="{bar_w}" height="{h}" fill="{border}"/>'
                    f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="{max(2, h - 4)}" fill="{fill}"/>'
                    f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="2" fill="{hi}"/>'
                )
            else:
                bar_parts.append(f'<rect x="{bx}" y="{by}" width="{bar_w}" height="2" fill="{border}"/>')
            bar_parts.append(render_pixel_text(day["label"], cx, 152, 2, lbl_col, align="center"))

        bar_parts_svg = "".join(bar_parts)

    f4 = anim_frame % 4
    sparkle_svg = (
        render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 222, 10 - (2 if f4 == 2 else 0), 2)
        if f4 in (0, 2)
        else _render_twinkle(224, 12, "#F8D030", alt=(f4 == 1))
    )

    sx, sy = 160 + dx, 14 + dy
    hero_y = 50 if hero_scale == 4 else 54
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="4" fill="{pal['top_band']}"/>
  {render_horizon_dither(72, pal['dither'])}
  {render_battle_pad(196, 74, pal['pad_top'], pal['pad_mid'], pal['pad_bot'], shadow_shrink=sh)}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_pixel_text("GEMINI", 28, 12, 2, pal['ink'])}
  {render_pixel_text(f"Lv{lv_val}", 146, 12, 2, pal['blue'], align="right")}

  {f'<g opacity="0.4">{render_sprite(sp_grid, sp_pal, sx, sy, 4)}</g>' if out_of_tokens else render_sprite(sp_grid, sp_pal, sx, sy, 4)}
  {(_render_defeated_fx(sx, sy, anim_frame) if out_of_tokens else _render_species_fx(sp_key, sx, sy, anim_frame))}
  {sparkle_svg}

  <rect x="14" y="30" width="22" height="12" fill="{pal['border']}"/>
  {render_pixel_text("HP", 18, 33, 1, "#F8C828")}
  <rect x="36" y="30" width="{hp_max_w + 4}" height="12" fill="{pal['border']}"/>
  <rect x="38" y="32" width="{hp_max_w}" height="8" fill="{pal['bg']}"/>
  <rect x="38" y="32" width="{hp_fill_w}" height="8" fill="{hp_color}"/>
  <rect x="38" y="32" width="{hp_fill_w}" height="2" fill="{hp_hi}"/>
  {render_pixel_text(f"{cache_pct}%", 146, 30, 2, pal['ink'], align="right")}

  {render_pixel_text(today_total_str, 18, hero_y, hero_scale, pal['ink'], shadow=pal['shadow'], shadow_offset=2)}
  {render_battle_bracket(8, 28, 134, 60, pal['border'], "#3898F8")}

  {_render_card_frame(8, 100, 224, 66, pal)}
  {render_pixel_text("EXP 7D", 16, 106, 2, pal['blue'])}
  {render_pixel_text(week_total_str, 224, 106, 2, pal['ink'], align="right")}
  {render_dotted_rule(16, 120, 208, pal['divider'])}
  {bar_parts_svg}

  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(f"IN:{in_str}", 20, 186, 2, pal['ink']) if not out_of_tokens else render_pixel_text(f"D:{in_str}", 20, 186, 2, pal['ink'])}
  {render_pixel_text(f"OUT:{out_str}", 216, 186, 2, pal['ink'], align="right") if not out_of_tokens else render_pixel_text(f"W:{out_str}", 216, 186, 2, pal['ink'], align="right")}
  {render_pixel_text(f"CCH:{cache_str}", 20, 206, 2, pal['blue'])}
  {render_pixel_text(updated_hm, 200, 206, 2, pal['muted'], align="right")}
  {render_pixel_text(f"D:{daily_reset}", 20, 222, 2, pal['muted']) if not out_of_tokens else ""}
  {render_pixel_text(f"W:{weekly_reset}", 216, 222, 2, pal['muted'], align="right") if not out_of_tokens else ""}
</svg>
'''


# ============================================================
# 1b. CLAUDE CODE TOKENS (Gen-2 Battle HUD)
# ============================================================
def build_claude_svg(stats: dict, cfg: dict | None = None, anim_frame: int = 0, now: datetime | None = None) -> str:
    """Build a Claude Code token usage screen — same battle HUD style as Gemini."""
    if now is None:
        now = datetime.now().astimezone()
    pal = resolve_palette(cfg, now)
    sprites = (cfg or {}).get("sprites") or {}
    sp_key, sp_grid, sp_pal, _ = _get_sprite(sprites.get("claude", "charmander"), "charmander")

    out_of_tokens = stats.get("out_of_tokens", False)

    if out_of_tokens:
        dx, dy, sh = 0, 20, 4
    else:
        dx, dy, sh = _pokemon_motion(sp_key, anim_frame)

    today_total_str = fmt_tokens(stats["today_total"])
    week_total_str = fmt_tokens(stats["week_total"])
    updated_hm = stats["updated_hm"]
    daily_reset = stats.get("daily_reset", "--:--")
    weekly_reset = stats.get("weekly_reset", "--:--")

    if out_of_tokens:
        in_str = daily_reset
        out_str = weekly_reset
        cache_str = "--"
    else:
        in_str = fmt_tokens(stats["today_in"])
        out_str = fmt_tokens(stats["today_out"])
        cache_str = fmt_tokens(stats["today_cache"])
    steps = stats["today_steps"]
    lv_val = min(99, max(1, steps)) if steps > 0 else 1
    cache_pct = stats["cache_pct"]

    if cache_pct >= 50:
        hp_color, hp_hi = "#30B848", "#78E878"
    elif cache_pct >= 20:
        hp_color, hp_hi = "#F8C828", "#FFF080"
    else:
        hp_color, hp_hi = "#E83838", "#F88888"

    hp_max_w = 60
    hp_fill_w = (max(2, int(round((cache_pct / 100.0) * hp_max_w))) // 2) * 2 if cache_pct > 0 else 0
    hero_scale = 4 if pixel_text_width(today_total_str, 4) <= 122 else 3

    # Build bars or "SIN TOKENS" message
    if out_of_tokens:
        f4 = anim_frame % 4
        show_msg = f4 < 3
        if show_msg:
            bar_parts_svg = render_pixel_text("SIN TOKENS", 120, 130, 3, "#E83838", align="center", shadow=pal['shadow'], shadow_offset=2)
            bar_parts_svg += render_pixel_text("ESPERA MAÑANA", 120, 152, 2, pal['muted'], align="center")
        else:
            bar_parts_svg = render_pixel_text("SIN TOKENS", 120, 130, 3, pal['muted'], align="center")
            bar_parts_svg += render_pixel_text("ESPERA MAÑANA", 120, 152, 2, pal['bar_empty'], align="center")
    else:
        max_day = max((d["total"] for d in stats["week_days"]), default=0)
        bar_parts: list[str] = []
        start_x, slot_w, bar_w, base_y, max_h = 20, 29, 18, 148, 24

        for idx, day in enumerate(stats["week_days"]):
            bx = start_x + idx * slot_w
            cx = bx + bar_w // 2
            val = day["total"]
            if max_day > 0 and val > 0:
                h = (max(6, int(round((val / max_day) * max_h))) // 2) * 2
            else:
                h = 2
            by = base_y - h
            if day["is_today"]:
                fill, hi, border, lbl_col = "#F8B800", "#FFF090", pal["border"], pal["ink"]
            elif val > 0:
                fill, hi, border, lbl_col = "#A855F7", "#D8B4FE", pal["border"], pal["muted"]
            else:
                fill, hi, border, lbl_col = pal["bar_empty"], pal["bar_empty"], pal["muted"], pal["muted"]

            if h >= 6:
                bar_parts.append(
                    f'<rect x="{bx}" y="{by}" width="{bar_w}" height="{h}" fill="{border}"/>'
                    f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="{max(2, h - 4)}" fill="{fill}"/>'
                    f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="2" fill="{hi}"/>'
                )
            else:
                bar_parts.append(f'<rect x="{bx}" y="{by}" width="{bar_w}" height="2" fill="{border}"/>')
            bar_parts.append(render_pixel_text(day["label"], cx, 152, 2, lbl_col, align="center"))

        bar_parts_svg = "".join(bar_parts)

    f4 = anim_frame % 4
    sparkle_svg = (
        render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 222, 10 - (2 if f4 == 2 else 0), 2)
        if f4 in (0, 2)
        else _render_twinkle(224, 12, "#F8D030", alt=(f4 == 1))
    )

    sx, sy = 160 + dx, 14 + dy
    hero_y = 50 if hero_scale == 4 else 54
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="4" fill="{pal['top_band']}"/>
  {render_horizon_dither(72, pal['dither'])}
  {render_battle_pad(196, 74, pal['pad_top'], pal['pad_mid'], pal['pad_bot'], shadow_shrink=sh)}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_pixel_text("CLAUDE", 28, 12, 2, pal['ink'])}
  {render_pixel_text(f"Lv{lv_val}", 146, 12, 2, pal['blue'], align="right")}

  {f'<g opacity="0.4">{render_sprite(sp_grid, sp_pal, sx, sy, 4)}</g>' if out_of_tokens else render_sprite(sp_grid, sp_pal, sx, sy, 4)}
  {(_render_defeated_fx(sx, sy, anim_frame) if out_of_tokens else _render_species_fx(sp_key, sx, sy, anim_frame))}
  {sparkle_svg}

  <rect x="14" y="30" width="22" height="12" fill="{pal['border']}"/>
  {render_pixel_text("HP", 18, 33, 1, "#F8C828")}
  <rect x="36" y="30" width="{hp_max_w + 4}" height="12" fill="{pal['border']}"/>
  <rect x="38" y="32" width="{hp_max_w}" height="8" fill="{pal['bg']}"/>
  <rect x="38" y="32" width="{hp_fill_w}" height="8" fill="{hp_color}"/>
  <rect x="38" y="32" width="{hp_fill_w}" height="2" fill="{hp_hi}"/>
  {render_pixel_text(f"{cache_pct}%", 146, 30, 2, pal['ink'], align="right")}

  {render_pixel_text(today_total_str, 18, hero_y, hero_scale, pal['ink'], shadow=pal['shadow'], shadow_offset=2)}
  {render_battle_bracket(8, 28, 134, 60, pal['border'], "#A855F7")}

  {_render_card_frame(8, 100, 224, 66, pal)}
  {render_pixel_text("EXP 7D", 16, 106, 2, pal['blue'])}
  {render_pixel_text(week_total_str, 224, 106, 2, pal['ink'], align="right")}
  {render_dotted_rule(16, 120, 208, pal['divider'])}
  {bar_parts_svg}

  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(f"IN:{in_str}", 20, 186, 2, pal['ink']) if not out_of_tokens else render_pixel_text(f"D:{in_str}", 20, 186, 2, pal['ink'])}
  {render_pixel_text(f"OUT:{out_str}", 216, 186, 2, pal['ink'], align="right") if not out_of_tokens else render_pixel_text(f"W:{out_str}", 216, 186, 2, pal['ink'], align="right")}
  {render_pixel_text(f"CCH:{cache_str}", 20, 206, 2, pal['blue'])}
  {render_pixel_text(updated_hm, 200, 206, 2, pal['muted'], align="right")}
  {render_pixel_text(f"D:{daily_reset}", 20, 222, 2, pal['muted']) if not out_of_tokens else ""}
  {render_pixel_text(f"W:{weekly_reset}", 216, 222, 2, pal['muted'], align="right") if not out_of_tokens else ""}
</svg>
'''


# ============================================================
# 2. POKÉGEAR CLOCK (Authentic Gen-2 Pokégear + Day/Night)
# ============================================================
def build_clock_svg(
    now: datetime | None = None,
    device_cfg: dict | None = None,
    weather: dict | None = None,
    anim_frame: int = 0,
) -> str:
    if now is None:
        now = datetime.now().astimezone()
    device_cfg = device_cfg or {}
    pal = resolve_palette(device_cfg, now)
    sprites = device_cfg.get("sprites") or {}
    is_night = now.hour >= 19 or now.hour < 6
    hour12 = bool(device_cfg.get("hour12", True))
    celsius = bool(device_cfg.get("celsius", True))

    if hour12:
        h = now.hour % 12 or 12
        time_str = f"{h:02d}:{now.minute:02d}"
        ampm = "PM" if now.hour >= 12 else "AM"
    else:
        time_str = now.strftime("%H:%M")
        ampm = ""

    day_name = DAYS_SHORT_ES[now.weekday()]
    mon_name = MONTHS_SHORT_ES[now.month - 1]
    date_str = f"{day_name} {now.day:02d} {mon_name} {now.year}"

    header_title = truncate_pixel_text(str(device_cfg.get("trainer_name") or "POKEGEAR"), 94, 2) or "POKEGEAR"
    city = truncate_pixel_text(str(device_cfg.get("city") or "PARACAS"), 102, 2)
    temp_str = ""
    cond_short = ""
    if weather and weather.get("temp") is not None:
        t = weather["temp"]
        if not celsius:
            t = t * 9 / 5 + 32
        unit = "C" if celsius else "F"
        temp_str = f"{int(round(t))}°{unit}"
        avail_cond_px = max(72, 174 - pixel_text_width(city, 2))
        cond_short = truncate_pixel_text(str(weather.get("description") or ""), avail_cond_px, 2)

    f4 = anim_frame % 4
    sky_bob = -2 if f4 in (1, 2) else 0
    if is_night:
        sp_key, sp_grid, sp_pal, _ = _get_sprite(sprites.get("clock_night", "gengar"), "gengar")
        badge_bg = "#483878"
        badge_txt = "NOCHE"
        badge_w = 68
        sky_icon = (
            render_sprite(MINI_MOON, MINI_MOON_PAL, 130, 12 + sky_bob, 2)
            + _render_twinkle(226, 12, "#F8E878", alt=(f4 in (1, 3)))
        )
        accent = "#7858A8"
    else:
        sp_key, sp_grid, sp_pal, _ = _get_sprite(sprites.get("clock_day", "bulbasaur"), "bulbasaur")
        badge_bg = "#249440"
        badge_txt = "DIA"
        badge_w = 48
        sky_icon = (
            render_sprite(MINI_SUN, MINI_SUN_PAL, 128, 10 + sky_bob, 2)
            + _render_twinkle(226, 12, "#F8C828", alt=(f4 in (1, 3)))
        )
        accent = "#249440"

    dx, dy, sh = _pokemon_motion(sp_key, anim_frame)
    sx, sy = 160 + dx, 14 + dy

    # Top-left secondary stat: live temp (or short date fallback if weather unavailable)
    hud_stat = temp_str or f"{now.day:02d}/{now.month:02d}"
    clock_x = 18 if ampm else 42
    ampm_svg = ""
    if ampm:
        ampm_svg = (
            f'<rect x="184" y="132" width="38" height="22" fill="{pal["border"]}"/>'
            f'<rect x="186" y="134" width="34" height="18" fill="{badge_bg}"/>'
            f'{render_pixel_text(ampm, 203, 137, 2, "#FFFFFF", align="center")}'
        )

    bottom_right = cond_short if (cond_short and temp_str) else temp_str
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="4" fill="{accent}"/>
  {render_horizon_dither(72, pal['dither'])}
  {render_battle_pad(196, 74, pal['pad_top'], pal['pad_mid'], pal['pad_bot'], shadow_shrink=sh)}

  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_pixel_text(header_title, 28, 12, 2, pal['ink'])}
  {sky_icon}

  {render_sprite(sp_grid, sp_pal, sx, sy, 4)}
  {_render_species_fx(sp_key, sx, sy, anim_frame)}

  <rect x="16" y="34" width="{badge_w}" height="16" fill="{badge_bg}"/>
  {render_pixel_text(badge_txt, 16 + badge_w // 2, 36, 2, "#FFFFFF", align="center")}
  {render_signal_bars(132, 37, accent)}

  {render_pixel_text(hud_stat, 18, 58, 3, pal['ink'], shadow=pal['shadow'], shadow_offset=2)}
  {render_battle_bracket(8, 30, 134, 58, pal['border'], accent)}

  {_render_card_frame(8, 100, 224, 66, pal, top_accent=accent)}
  {render_pixel_text(time_str, clock_x, 118, 6, pal['ink'], shadow=pal['shadow'], shadow_offset=2)}
  {ampm_svg}

  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(date_str, 20, 186, 2, pal['ink'])}
  {render_pixel_text(city, 20, 206, 2, pal['blue'])}
  {render_pixel_text(bottom_right, 202, 206, 2, pal['muted'], align="right")}
</svg>
'''


# ============================================================
# 3. POKÉ WEATHER (Dynamic Sprite + 3-Day Forecast)
# ============================================================
def _weather_pokemon(main: str, override: str = "auto") -> tuple[str, list[str], dict[str, str], str]:
    if override and override != "auto" and override in SPRITE_CATALOG:
        return _get_sprite(override, "snorlax")
    m = (main or "").lower()
    if any(k in m for k in ("rain", "drizzle", "thunder", "storm")):
        return "squirtle", SQUIRTLE_SPRITE, SQUIRTLE_PALETTE, "#3898F8"
    if "clear" in m or "sun" in m:
        return "charmander", CHARMANDER_SPRITE, CHARMANDER_PALETTE, "#F87828"
    return "snorlax", SNORLAX_SPRITE, SNORLAX_PALETTE, "#387888"


def _mini_weather_icon(main: str, x: int, y: int) -> str:
    m = (main or "").lower()
    if any(k in m for k in ("rain", "drizzle", "thunder", "storm")):
        return render_sprite(MINI_RAIN, MINI_RAIN_PAL, x, y, 2)
    if "clear" in m or "sun" in m:
        return render_sprite(MINI_SUN, MINI_SUN_PAL, x, y, 2)
    return render_sprite(MINI_CLOUD, MINI_CLOUD_PAL, x, y, 2)


def _weather_particles(main: str, anim_frame: int) -> str:
    f4 = anim_frame % 4
    m = (main or "").lower()
    if any(k in m for k in ("rain", "drizzle", "thunder", "storm")):
        drops = [(158, 12 + f4 * 16), (226, 18 + ((f4 + 2) % 4) * 14), (232, 10 + ((f4 + 1) % 4) * 16)]
        return "".join(f'<rect x="{x}" y="{y}" width="2" height="6" fill="#3898F8"/>' for x, y in drops)
    if "clear" in m or "sun" in m:
        return (
            _render_twinkle(224, 12, "#F8B800", alt=(f4 in (0, 2)))
            + _render_twinkle(154, 14, "#F87828", alt=(f4 in (1, 3)))
        )
    shift = [0, 2, 4, 2][f4]
    return (
        f'<rect x="{218 + shift}" y="12" width="12" height="4" fill="#B0C2D4"/>'
        f'<rect x="{220 + shift}" y="10" width="8" height="2" fill="#DCE6F0"/>'
    )


def build_weather_svg(
    weather: dict,
    device_cfg: dict,
    anim_frame: int = 0,
    now: datetime | None = None,
) -> str:
    if now is None:
        now = datetime.now().astimezone()
    device_cfg = device_cfg or {}
    pal = resolve_palette(device_cfg, now)
    sprites = device_cfg.get("sprites") or {}
    celsius = bool(device_cfg.get("celsius", True))
    unit = "C" if celsius else "F"

    def conv(t: float) -> int:
        return int(round(t if celsius else t * 9 / 5 + 32))

    city = truncate_pixel_text(str(weather.get("city") or device_cfg.get("city") or "PARACAS"), 114, 2)
    temp_val = conv(float(weather.get("temp", 20)))
    temp_str = f"{temp_val}°{unit}"
    hum = int(weather.get("humidity", 0))
    wind_kmh = int(round(float(weather.get("wind_ms", 0)) * 3.6))
    desc = truncate_pixel_text(str(weather.get("description") or "CLIMA"), 192, 2)
    main = str(weather.get("main") or "Clouds")
    updated_hm = str(weather.get("updated_hm") or "")

    sp_key, sprite_grid, sprite_pal, accent = _weather_pokemon(main, str(sprites.get("weather", "auto")))
    dx, dy, sh = _pokemon_motion(sp_key, anim_frame)
    sx, sy = 160 + dx, 14 + dy

    hum_max_w = 54
    hum_fill_w = (max(2, int(round((min(100, max(0, hum)) / 100.0) * hum_max_w))) // 2) * 2 if hum > 0 else 0

    fc_parts: list[str] = []
    days = (weather.get("forecast") or [])[:3]
    col_w = 72
    for idx, d in enumerate(days):
        cx = 12 + idx * col_w + col_w // 2
        if idx > 0:
            vx = 12 + idx * col_w
            fc_parts.append(f'<rect x="{vx}" y="108" width="2" height="50" fill="{pal["divider"]}"/>')
        lbl = sanitize_pixel_text(d.get("label", "---"))[:3]
        fc_parts.append(render_pixel_text(lbl, cx, 107, 2, pal["blue"], align="center"))
        fc_parts.append(_mini_weather_icon(d.get("main", ""), cx - 7, 123))
        tmax = conv(float(d.get("max", temp_val)))
        tmin = conv(float(d.get("min", temp_val)))
        hi_txt = f"{tmax}"
        lo_txt = f"/{tmin}"
        total_w = pixel_text_width(hi_txt, 2, gap=1) + pixel_text_width(lo_txt, 2, gap=1)
        tx = cx - total_w // 2
        fc_parts.append(render_pixel_text(hi_txt, tx, 146, 2, pal["ink"], gap=1))
        fc_parts.append(render_pixel_text(lo_txt, tx + pixel_text_width(hi_txt, 2, gap=1), 146, 2, pal["muted"], gap=1))

    if not fc_parts:
        fc_parts.append(render_pixel_text("SIN PRONOSTICO", 120, 128, 2, pal["muted"], align="center"))

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="4" fill="{accent}"/>
  {render_horizon_dither(72, pal['dither'])}
  {render_battle_pad(196, 74, pal['pad_top'], pal['pad_mid'], pal['pad_bot'], shadow_shrink=sh)}

  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_pixel_text(city, 28, 12, 2, pal['ink'])}
  {_weather_particles(main, anim_frame)}
  {render_sprite(sprite_grid, sprite_pal, sx, sy, 4)}
  {_render_species_fx(sp_key, sx, sy, anim_frame)}

  <rect x="14" y="30" width="26" height="12" fill="{pal['border']}"/>
  {render_pixel_text("HUM", 17, 33, 1, "#68D0FF")}
  <rect x="40" y="30" width="{hum_max_w + 4}" height="12" fill="{pal['border']}"/>
  <rect x="42" y="32" width="{hum_max_w}" height="8" fill="{pal['bg']}"/>
  <rect x="42" y="32" width="{hum_fill_w}" height="8" fill="#3898F8"/>
  <rect x="42" y="32" width="{hum_fill_w}" height="2" fill="#88D8FF"/>
  {render_pixel_text(f"{hum}%", 146, 30, 2, pal['ink'], align="right")}

  {render_pixel_text(temp_str, 18, 52, 4, pal['ink'], shadow=pal['shadow'], shadow_offset=2)}
  {_mini_weather_icon(main, 128, 60)}
  {render_battle_bracket(8, 28, 134, 60, pal['border'], accent)}

  {_render_card_frame(8, 100, 224, 66, pal)}
  {"".join(fc_parts)}

  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(desc, 20, 186, 2, pal['ink'])}
  {render_pixel_text(f"VNT {wind_kmh}KMH", 20, 206, 2, pal['blue'])}
  {render_pixel_text(updated_hm, 200, 206, 2, pal['muted'], align="right")}
</svg>
'''


# ============================================================
# 4. SPOTIFY NOW PLAYING (Gen-2 Concert Stage — Exclusive Auto)
# ============================================================
def build_spotify_svg(
    track: dict,
    cfg: dict | None = None,
    anim_frame: int = 0,
    now: datetime | None = None,
) -> str:
    if now is None:
        now = datetime.now().astimezone()
    pal = resolve_palette(cfg, now)
    sprites = (cfg or {}).get("sprites") or {}
    sp_key, sp_grid, sp_pal, _ = _get_sprite(sprites.get("spotify", "jigglypuff"), "jigglypuff")

    f6 = anim_frame % len(_SPOTIFY_EQ_FRAMES)
    dx, dy, sh = _pokemon_motion(sp_key, anim_frame)
    sx, sy = 158 + dx, 14 + dy
    note_l_dy = -2 if f6 in (1, 2, 4) else 0
    note_r_dy = 0 if f6 in (1, 2, 4) else -2

    title = str(track.get("title") or "SIN TITULO")
    artist = str(track.get("artist") or "ARTISTA")
    album = str(track.get("album") or "")

    title_lines = wrap_pixel_text(title, 196, 2, max_lines=2)
    artist_line = truncate_pixel_text(artist, 196, 2)
    album_line = truncate_pixel_text(album, 168, 2) if album else "EN REPRODUCCION"

    eq_heights = _SPOTIFY_EQ_FRAMES[f6]
    bgm_w = _SPOTIFY_BGM_WIDTHS[f6]
    eq_parts = []
    for idx, h in enumerate(eq_heights):
        ex = 18 + idx * 12
        ey = 82 - h
        mid_h = max(2, h - 4)
        low_h = max(2, mid_h // 2)
        eq_parts.append(
            f'<rect x="{ex}" y="{ey}" width="8" height="{h}" fill="{pal["border"]}"/>'
            f'<rect x="{ex + 2}" y="{ey + 2}" width="4" height="{mid_h}" fill="#1DB954"/>'
            f'<rect x="{ex + 2}" y="{80 - low_h}" width="4" height="{low_h}" fill="#14833F"/>'
            f'<rect x="{ex + 2}" y="{ey + 2}" width="4" height="2" fill="#88F8A8"/>'
        )
        if h >= 20:
            eq_parts.append(f'<rect x="{ex + 2}" y="{ey - 4}" width="4" height="2" fill="#88F8A8"/>')

    title_svg = render_pixel_text(title_lines[0], 20, 122, 2, pal["ink"])
    if len(title_lines) > 1:
        title_svg += render_pixel_text(title_lines[1], 20, 142, 2, pal["ink"])
        div_y, artist_y, album_y = 162, 174, 198
    else:
        div_y, artist_y, album_y = 148, 162, 190

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="4" fill="#1DB954"/>
  {render_horizon_dither(72, pal['dither'])}
  {render_battle_pad(196, 74, pal['pad_top'], pal['pad_mid'], pal['pad_bot'], shadow_shrink=sh)}

  {render_sprite(NOTE_SPRITE, NOTE_PALETTE, 8, 10 + note_l_dy, 2)}
  {render_pixel_text("SPOTIFY", 24, 12, 2, pal['ink'], gap=1)}
  <rect x="108" y="10" width="40" height="14" fill="#1DB954"/>
  {render_pixel_text("PLAY", 128, 14, 1, "#FFFFFF", align="center")}

  {render_sprite(sp_grid, sp_pal, sx, sy, 4)}
  {_render_species_fx(sp_key, sx, sy, anim_frame)}
  {render_sprite(NOTE_SPRITE, NOTE_PALETTE, 222, 10 + note_r_dy, 2)}

  <rect x="14" y="34" width="24" height="12" fill="{pal['border']}"/>
  {render_pixel_text("BGM", 17, 37, 1, "#88F8A8")}
  <rect x="38" y="34" width="106" height="12" fill="{pal['border']}"/>
  <rect x="40" y="36" width="102" height="8" fill="{pal['bg']}"/>
  <rect x="40" y="36" width="{bgm_w}" height="8" fill="#1DB954"/>
  <rect x="40" y="36" width="{bgm_w}" height="2" fill="#88F8A8"/>

  {"".join(eq_parts)}
  {render_battle_bracket(8, 30, 134, 58, pal['border'], "#1DB954")}

  {_render_dialogue_box_themed(8, 104, 224, 130, pal)}
  {title_svg}
  {render_dotted_rule(20, div_y, 200, pal['divider'])}
  {render_pixel_text(artist_line, 20, artist_y, 2, pal['green'])}
  {render_pixel_text(album_line, 20, album_y, 2, pal['muted'])}
</svg>
'''


# ============================================================
# 5. POKÉMON NOTIFICATION CARD (Gen-1/2 Encounter Alert)
# ============================================================
def build_notify_svg(
    title: str,
    message: str,
    pokemon: str = "pikachu",
    cfg: dict | None = None,
    anim_frame: int = 0,
    app_name: str = "",
    now: datetime | None = None,
) -> str:
    if now is None:
        now = datetime.now().astimezone()
    pal = resolve_palette(cfg, now)
    sp_key, sprite_grid, sprite_pal, accent = _get_sprite(pokemon, "pikachu")
    dx, dy, sh = _pokemon_motion(sp_key, anim_frame)
    sx, sy = 160 + dx, 14 + dy
    banner_text = truncate_pixel_text(app_name, 106, 2) if app_name else ""
    if not banner_text:
        banner_text = "AVISO!"
    title_clean = truncate_pixel_text(title or "AVISO!", 124, 2)
    msg_lines = wrap_pixel_text(message or "", 196, 2, max_lines=3)
    now_hm = now.strftime("%H:%M")

    f4 = anim_frame % 4
    sparkle_svg = (
        render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 222, 10 - (2 if f4 % 2 else 0), 2)
        if f4 in (0, 2)
        else _render_twinkle(224, 12, "#F8D030", alt=True)
    )

    msg_svg = ""
    for idx, line in enumerate(msg_lines):
        msg_svg += render_pixel_text(line, 20, 124 + idx * 26, 2, pal["ink"])

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="6" fill="{accent}"/>
  {render_horizon_dither(72, pal['dither'])}
  {render_battle_pad(196, 74, pal['pad_top'], pal['pad_mid'], pal['pad_bot'], shadow_shrink=sh)}

  <rect x="8" y="10" width="18" height="18" fill="{accent}"/>
  {render_pixel_text("!", 17, 13, 2, "#FFFFFF", align="center")}
  <rect x="30" y="10" width="114" height="18" fill="{pal['border']}"/>
  {render_pixel_text(banner_text, 87, 13, 2, "#F8D030", align="center")}

  {render_sprite(sprite_grid, sprite_pal, sx, sy, 4)}
  {_render_species_fx(sp_key, sx, sy, anim_frame)}
  {sparkle_svg}

  {render_pixel_text(title_clean, 18, 42, 2, pal['ink'])}
  {render_pixel_text(now_hm, 18, 66, 2, pal['muted'])}
  {render_battle_bracket(8, 32, 134, 56, pal['border'], accent)}

  {_render_dialogue_box_themed(8, 104, 224, 130, pal)}
  {msg_svg}
</svg>
'''
