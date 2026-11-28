"""
Authentic Pokémon GBA / DS Battle & Pokégear 240x240 Screens:
  1. gemini   — Pokémon Battle HUD (Antigravity Gemini Token Usage)
  2. claude   — Pokémon Battle HUD (Official Claude Code Rate Limits)
  3. clock    — Pokégear Day / Night Battle Stage + Digital Clock
  4. weather  — Dynamic Weather Battle Scene + 3-Day Forecast
  5. spotify  — Pokégear Radio / Jigglypuff Concert Stage
  6. notify   — Wild Encounter Alert Banner & RPG Dialogue Box
"""

from __future__ import annotations

from datetime import datetime

from official_sprites import OFFICIAL_SPRITES, render_official_pokemon
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
    MINI_STORM,
    MINI_STORM_PALETTE,
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
    render_dotted_rule,
    render_pixel_text,
    render_signal_bars,
    render_sprite,
    sanitize_pixel_text,
    truncate_pixel_text,
    wrap_pixel_text,
)

# All PC-side animated elements live strictly inside y = 0..111 (7x16 JPEG MCU boundary)
ANIM_STRIP_H = 112

DAYS_SHORT_ES = ["LUN", "MAR", "MIE", "JUE", "VIE", "SAB", "DOM"]
MONTHS_SHORT_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]

# Preserved 18x18 catalog for ESP8266 offline PROGMEM sprite generator compatibility
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

# Subtle 1-2px stage bounce paired with the 6-frame real sprite animations
_STAGE_BOB_4 = (0, -2, 0, -2)
_STAGE_BOB_6 = (0, -2, -2, 0, -2, 0)

# 6-frame smooth traveling Spotify equalizer wave (9 bars inside Battle Plate)
_SPOTIFY_EQ_FRAMES: list[list[int]] = [
    [8, 16, 22, 14, 24, 18, 12, 20, 10],
    [14, 22, 16, 24, 18, 12, 22, 14, 16],
    [20, 14, 10, 18, 24, 20, 16, 10, 20],
    [12, 8, 18, 24, 16, 24, 20, 14, 12],
    [16, 20, 24, 16, 10, 18, 22, 18, 14],
    [10, 18, 14, 10, 20, 22, 14, 22, 10],
]
_SPOTIFY_BGM_PCTS = (76, 88, 94, 82, 90, 72)

PALETTES: dict[str, dict[str, str]] = {
    "gbc_classic": {
        "name": "gbc_classic",
        "bg": "#E8F0E0",
        "deck_bg": "#D4E0C8",
        "card": "#F8F8F0",
        "plate_bg": "#F8F4D8",
        "plate_bevel": "#DFD6B0",
        "plate_shadow": "#D4CAA4",
        "card_shadow": "#D0D8C8",
        "ink": "#182028",
        "border": "#182028",
        "muted": "#586870",
        "shadow": "#B8C8AC",
        "divider": "#C4D0B8",
        "blue": "#2060C8",
        "green": "#188838",
        "top_band": "#3890F0",
        "frame_trim": "#3890F0",
        "sky_top": "#68B8F8",
        "sky_mid": "#90D0FF",
        "sky_bot": "#C8ECFF",
        "hill_dark": "#388840",
        "hill_light": "#58B050",
        "grass_top": "#88D868",
        "grass_bot": "#68BC50",
        "pad_top": "#409038",
        "pad_mid": "#A8F080",
        "pad_hi": "#CEFCA8",
        "pad_bot": "#286828",
        "bar_empty": "#C8D4C0",
        "dither": "#B0E090",
    },
    "gbc_dark": {
        "name": "gbc_dark",
        "bg": "#0E1526",
        "deck_bg": "#0A1020",
        "card": "#162038",
        "plate_bg": "#182440",
        "plate_bevel": "#263860",
        "plate_shadow": "#0A1020",
        "card_shadow": "#0A1020",
        "ink": "#F0F4FF",
        "border": "#7088B8",
        "muted": "#8EA2CC",
        "shadow": "#060A14",
        "divider": "#28385C",
        "blue": "#68B0FF",
        "green": "#38D890",
        "top_band": "#5878C8",
        "frame_trim": "#5878C8",
        "sky_top": "#0C1228",
        "sky_mid": "#162044",
        "sky_bot": "#223262",
        "hill_dark": "#0E1A32",
        "hill_light": "#182A4C",
        "grass_top": "#1E3458",
        "grass_bot": "#162846",
        "pad_top": "#2C4878",
        "pad_mid": "#3C6098",
        "pad_hi": "#527AB8",
        "pad_bot": "#182848",
        "bar_empty": "#243250",
        "dither": "#1A2C4C",
    },
    "gb_green": {
        "name": "gb_green",
        "bg": "#9BBC0F",
        "deck_bg": "#8BAC0F",
        "card": "#9BBC0F",
        "plate_bg": "#9BBC0F",
        "plate_bevel": "#8BAC0F",
        "plate_shadow": "#306230",
        "card_shadow": "#306230",
        "ink": "#0F380F",
        "border": "#0F380F",
        "muted": "#306230",
        "shadow": "#306230",
        "divider": "#306230",
        "blue": "#0F380F",
        "green": "#0F380F",
        "top_band": "#306230",
        "frame_trim": "#306230",
        "sky_top": "#9BBC0F",
        "sky_mid": "#9BBC0F",
        "sky_bot": "#8BAC0F",
        "hill_dark": "#0F380F",
        "hill_light": "#306230",
        "grass_top": "#8BAC0F",
        "grass_bot": "#306230",
        "pad_top": "#306230",
        "pad_mid": "#9BBC0F",
        "pad_hi": "#9BBC0F",
        "pad_bot": "#0F380F",
        "bar_empty": "#306230",
        "dither": "#8BAC0F",
    },
    "pokedex_red": {
        "name": "pokedex_red",
        "bg": "#DC2830",
        "deck_bg": "#B81D24",
        "card": "#FFFDF8",
        "plate_bg": "#FFF8E4",
        "plate_bevel": "#E6D8B8",
        "plate_shadow": "#DECFA8",
        "card_shadow": "#E4D4CC",
        "ink": "#1C1820",
        "border": "#1C1820",
        "muted": "#685860",
        "shadow": "#88141A",
        "divider": "#E4CFC8",
        "blue": "#D02830",
        "green": "#188838",
        "top_band": "#DC2830",
        "frame_trim": "#E03038",
        "sky_top": "#70C0F8",
        "sky_mid": "#98D8FF",
        "sky_bot": "#D0F0FF",
        "hill_dark": "#388840",
        "hill_light": "#58B050",
        "grass_top": "#88D868",
        "grass_bot": "#68BC50",
        "pad_top": "#409038",
        "pad_mid": "#A8F080",
        "pad_hi": "#CEFCA8",
        "pad_bot": "#286828",
        "bar_empty": "#E4D0CA",
        "dither": "#B0E090",
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


def _resolve_species(name: str, fallback: str = "pikachu") -> tuple[str, str]:
    key = (name or "").strip().lower()
    if key not in OFFICIAL_SPRITES:
        key = fallback
    return key, OFFICIAL_SPRITES[key]["accent"]


def _render_twinkle(x: int, y: int, color: str, alt: bool = False) -> str:
    if alt:
        return (
            f'<rect x="{x + 2}" y="{y}" width="2" height="6" fill="{color}"/>'
            f'<rect x="{x}" y="{y + 2}" width="6" height="2" fill="{color}"/>'
        )
    return f'<rect x="{x + 2}" y="{y + 2}" width="2" height="2" fill="{color}"/>'


def _render_pixel_cloud(x: int, y: int, main_col: str = "#FFFFFF", shade_col: str = "#D0E8FC") -> str:
    return (
        f'<rect x="{x + 6}" y="{y}" width="16" height="4" fill="{main_col}"/>'
        f'<rect x="{x + 2}" y="{y + 4}" width="26" height="4" fill="{main_col}"/>'
        f'<rect x="{x}" y="{y + 8}" width="30" height="4" fill="{shade_col}"/>'
    )


def _render_battle_scene(
    pal: dict[str, str],
    anim_frame: int = 0,
    night_override: bool = False,
    weather_mode: str = "",
) -> str:
    """Authentic Pokémon GBA/DS outdoor battle arena backdrop inside y = 0..111."""
    pal_name = pal.get("name", "gbc_classic")
    is_gb = pal_name == "gb_green"
    is_dark = pal_name == "gbc_dark" or (night_override and not is_gb)
    is_pokedex = pal_name == "pokedex_red"

    if is_dark:
        sky_t, sky_m, sky_b = "#0C142C", "#162248", "#223468"
        hill_d, hill_l = "#0E1C36", "#182C52"
        gr_t, gr_b = "#1E365C", "#162848"
        pad_t, pad_m, pad_h, pad_b = "#2C4C7E", "#3E66A2", "#5682C0", "#162644"
    else:
        sky_t, sky_m, sky_b = pal["sky_top"], pal["sky_mid"], pal["sky_bot"]
        hill_d, hill_l = pal["hill_dark"], pal["hill_light"]
        gr_t, gr_b = pal["grass_top"], pal["grass_bot"]
        pad_t, pad_m, pad_h, pad_b = pal["pad_top"], pal["pad_mid"], pal["pad_hi"], pal["pad_bot"]

    w_low = (weather_mode or "").lower()
    if not is_gb and not is_dark and any(k in w_low for k in ("rain", "drizzle", "thunder", "storm")):
        sky_t, sky_m, sky_b = "#4C6484", "#6882A4", "#8CA4C0"

    f4 = anim_frame % 4
    parts: list[str] = [
        # Lower Pokégear/HUD deck background (y = 112..240)
        f'<rect x="0" y="112" width="240" height="128" fill="{pal["deck_bg"]}"/>',
        # Layered pixel sky (y = 0..76)
        f'<rect x="0" y="0" width="240" height="26" fill="{sky_t}"/>',
        f'<rect x="0" y="26" width="240" height="26" fill="{sky_m}"/>',
        f'<rect x="0" y="52" width="240" height="24" fill="{sky_b}"/>',
    ]

    # Sky details: stars + crescent moon at night, drifting pixel clouds by day
    if is_dark:
        stars = ((148, 10), (174, 20), (210, 8), (228, 24), (158, 38), (222, 44))
        for idx, (sx, sy) in enumerate(stars):
            parts.append(_render_twinkle(sx, sy, "#FFF498" if (idx + f4) % 2 == 0 else "#98B8F8", alt=((idx + f4) % 3 == 0)))
        parts.append(render_sprite(MINI_MOON, MINI_MOON_PAL, 214, 10 + (-2 if f4 in (1, 2) else 0), 2))
    elif not is_gb:
        drift = (0, 2, 4, 2)[f4]
        if any(k in w_low for k in ("rain", "drizzle", "thunder", "storm")):
            parts.append(_render_pixel_cloud(146 + drift, 8, "#B8C8D8", "#889CB0"))
            parts.append(_render_pixel_cloud(198 - drift, 16, "#C8D6E4", "#94A8BC"))
            drops = ((156, 26 + f4 * 14), (186, 30 + ((f4 + 2) % 4) * 14), (218, 24 + ((f4 + 1) % 4) * 14), (232, 34 + f4 * 12))
            for dx_p, dy_p in drops:
                parts.append(f'<rect x="{dx_p}" y="{min(96, dy_p)}" width="2" height="6" fill="#58B8FF"/>')
        else:
            parts.append(_render_pixel_cloud(148 + drift, 10, "#FFFFFF", "#D4ECFF"))
            parts.append(_render_pixel_cloud(202 - drift, 20, "#FFFFFF", "#D4ECFF"))

    # Distant rolling pixel hills & treeline silhouette (y = 68..82)
    parts.extend([
        f'<rect x="0" y="72" width="240" height="10" fill="{hill_l}"/>',
        f'<rect x="140" y="68" width="36" height="6" fill="{hill_l}"/>',
        f'<rect x="192" y="66" width="48" height="8" fill="{hill_l}"/>',
        f'<rect x="0" y="78" width="240" height="4" fill="{hill_d}"/>',
        # Lush battle meadow ground (y = 82..110)
        f'<rect x="0" y="82" width="240" height="16" fill="{gr_t}"/>',
        f'<rect x="0" y="98" width="240" height="12" fill="{gr_b}"/>',
        # Crisp horizon divider at y = 110..112 (exact strip seam)
        f'<rect x="0" y="110" width="240" height="2" fill="{pal["border"]}"/>',
    ])

    # Authentic Gen-3/Gen-4 elliptical Pokémon battleground platform centered at (cx=192, y=86..107)
    cx = 192
    parts.extend([
        f'<rect x="{cx - 34}" y="86" width="68" height="2" fill="{pad_t}"/>',
        f'<rect x="{cx - 42}" y="88" width="84" height="4" fill="{pad_m}"/>',
        f'<rect x="{cx - 46}" y="92" width="92" height="10" fill="{pad_h}"/>',
        f'<rect x="{cx - 42}" y="94" width="84" height="6" fill="{pad_m}"/>',
        # Ground contact shadow beneath the Pokémon's feet
        f'<rect x="{cx - 26}" y="96" width="52" height="4" fill="{pad_t}"/>',
        f'<rect x="{cx - 42}" y="102" width="84" height="3" fill="{pad_t}"/>',
        f'<rect x="{cx - 34}" y="105" width="68" height="2" fill="{pad_b}"/>',
    ])

    # Optional Kanto Pokédex top bezel LEDs when pokedex_red theme is selected
    if is_pokedex:
        parts.extend([
            '<rect x="0" y="0" width="240" height="6" fill="#DC2830"/>',
            '<rect x="0" y="6" width="240" height="2" fill="#88141A"/>',
            '<rect x="214" y="1" width="4" height="4" fill="#58E8FF"/>',
            '<rect x="222" y="1" width="4" height="4" fill="#F8E038"/>',
            '<rect x="230" y="1" width="4" height="4" fill="#38E060"/>',
        ])

    return "".join(parts)


def _render_battle_status_plate(
    x: int,
    y: int,
    w: int,
    h: int,
    pal: dict[str, str],
    exp_pct: float | None = None,
    exp_color: str = "#38C0F8",
) -> str:
    """Authentic Pokémon Gen-3/Gen-4 beveled battle status plate with optional EXP strip."""
    b = pal["border"]
    bg = pal["plate_bg"]
    bev = pal["plate_bevel"]
    parts = [
        # Drop shadow
        f'<rect x="{x + 3}" y="{y + 3}" width="{w - 2}" height="{h - 2}" fill="#182028" opacity="0.35"/>',
        # Chamfered outer dark border
        f'<rect x="{x + 2}" y="{y}" width="{w - 4}" height="{h}" fill="{b}"/>',
        f'<rect x="{x}" y="{y + 2}" width="{w}" height="{h - 4}" fill="{b}"/>',
        # Bevel + cream parchment interior
        f'<rect x="{x + 2}" y="{y + 2}" width="{w - 4}" height="{h - 4}" fill="{bev}"/>',
        f'<rect x="{x + 4}" y="{y + 4}" width="{w - 8}" height="{h - 10}" fill="{bg}"/>',
        # Iconic right-bottom tail tab
        f'<rect x="{x + w - 2}" y="{y + h - 8}" width="4" height="6" fill="{b}"/>',
    ]
    if exp_pct is not None:
        bar_x = x + 26
        bar_y = y + h - 7
        bar_w = max(24, w - 34)
        fill_w = int(round((max(0.0, min(100.0, exp_pct)) / 100.0) * bar_w))
        parts.extend([
            render_pixel_text("EXP", x + 6, bar_y, 1, pal["muted"]),
            f'<rect x="{bar_x}" y="{bar_y + 1}" width="{bar_w}" height="4" fill="#202830"/>',
            f'<rect x="{bar_x + 1}" y="{bar_y + 2}" width="{max(0, bar_w - 2)}" height="2" fill="{pal["bar_empty"]}"/>',
        ])
        if fill_w > 0:
            parts.append(f'<rect x="{bar_x + 1}" y="{bar_y + 2}" width="{min(bar_w - 2, fill_w)}" height="2" fill="{exp_color}"/>')
    return "".join(parts)


def _render_hp_bar(
    x: int,
    y: int,
    total_w: int,
    pct: float | None,
    pal: dict[str, str],
    label: str = "HP",
    invert_danger: bool = False,
) -> str:
    """Authentic Pokémon dark-pill HP gauge (`[HP |████████░░]`)."""
    lbl_w = max(18, pixel_text_width(label, 1) + 6)
    track_w = max(24, total_w - lbl_w - 4)
    if pct is None:
        fill_w = 0
        c_main, c_hi = pal["bar_empty"], pal["bar_empty"]
    else:
        clamped = max(0.0, min(100.0, float(pct)))
        fill_w = int(round((clamped / 100.0) * track_w))
        if clamped > 0 and fill_w < 2:
            fill_w = 2
        if invert_danger:
            if clamped >= 85:
                c_main, c_hi = "#F04038", "#FF9890"
            elif clamped >= 60:
                c_main, c_hi = "#F8C828", "#FFF088"
            else:
                c_main, c_hi = "#38D858", "#90F898"
        else:
            if clamped >= 50:
                c_main, c_hi = "#38D858", "#90F898"
            elif clamped >= 20:
                c_main, c_hi = "#F8C828", "#FFF088"
            else:
                c_main, c_hi = "#F04038", "#FF9890"

    parts = [
        f'<rect x="{x}" y="{y}" width="{total_w}" height="10" fill="#202830"/>',
        f'<rect x="{x + 1}" y="{y + 1}" width="{total_w - 2}" height="8" fill="#202830"/>',
        render_pixel_text(label, x + 3, y + 2, 1, "#F8C838"),
        f'<rect x="{x + lbl_w}" y="{y + 2}" width="{track_w}" height="6" fill="#404850"/>',
    ]
    if fill_w > 0:
        parts.extend([
            f'<rect x="{x + lbl_w}" y="{y + 2}" width="{fill_w}" height="6" fill="{c_main}"/>',
            f'<rect x="{x + lbl_w}" y="{y + 2}" width="{fill_w}" height="2" fill="{c_hi}"/>',
        ])
    return "".join(parts)


def _render_poke_window(x: int, y: int, w: int, h: int, pal: dict[str, str], accent: str | None = None) -> str:
    """Classic Pokémon Emerald / HGSS framed window with colored inner trim."""
    b = pal["border"]
    trim = accent or pal["frame_trim"]
    card = pal["card"]
    return (
        f'<rect x="{x + 2}" y="{y}" width="{w - 4}" height="{h}" fill="{b}"/>'
        f'<rect x="{x}" y="{y + 2}" width="{w}" height="{h - 4}" fill="{b}"/>'
        f'<rect x="{x + 2}" y="{y + 2}" width="{w - 4}" height="{h - 4}" fill="{trim}"/>'
        f'<rect x="{x + 4}" y="{y + 4}" width="{w - 8}" height="{h - 8}" fill="{b}"/>'
        f'<rect x="{x + 6}" y="{y + 6}" width="{w - 12}" height="{h - 12}" fill="{card}"/>'
    )


def _render_dialogue_box_themed(x: int, y: int, w: int, h: int, pal: dict[str, str], cursor: bool = True) -> str:
    """Authentic Pokémon RPG double-border notched dialogue box."""
    b = pal["border"]
    bg = pal["card"]
    trim = pal["frame_trim"]
    parts = [
        f'<rect x="{x + 2}" y="{y}" width="{w - 4}" height="{h}" fill="{b}"/>',
        f'<rect x="{x}" y="{y + 2}" width="{w}" height="{h - 4}" fill="{b}"/>',
        f'<rect x="{x + 2}" y="{y + 2}" width="{w - 4}" height="{h - 4}" fill="{trim}"/>',
        f'<rect x="{x + 5}" y="{y + 5}" width="{w - 10}" height="{h - 10}" fill="{b}"/>',
        f'<rect x="{x + 7}" y="{y + 7}" width="{w - 14}" height="{h - 14}" fill="{bg}"/>',
        # Corner pixel ornaments
        f'<rect x="{x + 2}" y="{y + 2}" width="5" height="5" fill="{b}"/>',
        f'<rect x="{x + 3}" y="{y + 3}" width="2" height="2" fill="{trim}"/>',
        f'<rect x="{x + w - 7}" y="{y + 2}" width="5" height="5" fill="{b}"/>',
        f'<rect x="{x + w - 5}" y="{y + 3}" width="2" height="2" fill="{trim}"/>',
        f'<rect x="{x + 2}" y="{y + h - 7}" width="5" height="5" fill="{b}"/>',
        f'<rect x="{x + 3}" y="{y + h - 5}" width="2" height="2" fill="{trim}"/>',
        f'<rect x="{x + w - 7}" y="{y + h - 7}" width="5" height="5" fill="{b}"/>',
        f'<rect x="{x + w - 5}" y="{y + h - 5}" width="2" height="2" fill="{trim}"/>',
    ]
    if cursor:
        cx = x + w - 20
        cy = y + h - 16
        parts.extend([
            f'<rect x="{cx}" y="{cy}" width="10" height="2" fill="#E83838"/>',
            f'<rect x="{cx + 2}" y="{cy + 2}" width="6" height="2" fill="#E83838"/>',
            f'<rect x="{cx + 4}" y="{cy + 4}" width="2" height="2" fill="#E83838"/>',
        ])
    return "".join(parts)


def _render_stage_pokemon(
    sp_key: str,
    anim_frame: int,
    defeated: bool = False,
    cx: int = 192,
    base_feet_y: int = 100,
    bob_seq: tuple[int, ...] = _STAGE_BOB_4,
) -> str:
    """Render official multi-frame animated Pokémon on the battle pad (strictly inside y <= 110)."""
    if defeated:
        rects_svg, sx, sy, pw, _ = render_official_pokemon(sp_key, cx=cx, feet_y=min(108, base_feet_y + 6), anim_frame=0)
        f4 = anim_frame % 4
        star_y = max(4, sy - 6)
        stars: list[str] = []
        for idx, stx in enumerate((sx + 6, sx + pw // 2 - 3, sx + pw - 12)):
            yy = star_y - (2 if (f4 + idx) % 2 == 0 else 0)
            stars.append(
                f'<rect x="{stx + 2}" y="{yy}" width="2" height="6" fill="#F8E030"/>'
                f'<rect x="{stx}" y="{yy + 2}" width="6" height="2" fill="#F8E030"/>'
            )
        return f'<g opacity="0.55">{rects_svg}</g>{"".join(stars)}'

    bob = bob_seq[anim_frame % len(bob_seq)]
    rects_svg, _, _, _, _ = render_official_pokemon(sp_key, cx=cx, feet_y=base_feet_y + bob, anim_frame=anim_frame)
    return rects_svg


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
# 1. GEMINI TOKENS (Pokémon Battle HUD)
# ============================================================
def build_gemini_svg(stats: dict, cfg: dict | None = None, anim_frame: int = 0, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now().astimezone()
    pal = resolve_palette(cfg, now)
    sprites = (cfg or {}).get("sprites") or {}
    sp_key, accent = _resolve_species(sprites.get("gemini", "pikachu"), "pikachu")
    out_of_tokens = bool(stats.get("out_of_tokens", False))

    today_total_str = fmt_tokens(stats["today_total"])
    week_total_str = fmt_tokens(stats["week_total"])
    updated_hm = stats["updated_hm"]
    daily_reset = stats.get("daily_reset", "--:--")
    weekly_reset = stats.get("weekly_reset", "--:--")

    in_str = fmt_tokens(stats["today_in"])
    out_str = fmt_tokens(stats["today_out"])
    cache_str = fmt_tokens(stats["today_cache"])
    steps = stats["today_steps"]
    lv_val = min(99, max(1, steps)) if steps > 0 else 1
    cache_pct = int(stats["cache_pct"])

    hero_scale = 3 if pixel_text_width(today_total_str, 3) <= 118 else 2
    exp_pct = min(100.0, (stats["today_total"] / max(1, stats["week_total"])) * 100.0) if stats["week_total"] > 0 else 0.0

    if out_of_tokens:
        f4 = anim_frame % 4
        msg_col = "#E83838" if f4 < 3 else pal["muted"]
        bar_parts_svg = (
            render_pixel_text("SIN TOKENS!", 120, 134, 2, msg_col, align="center")
            + render_pixel_text("POKEMON DEBILITADO", 120, 152, 1, pal["muted"], align="center")
        )
    else:
        max_day = max((d["total"] for d in stats["week_days"]), default=0)
        bar_parts: list[str] = []
        start_x, slot_w, bar_w, base_y, max_h = 18, 29, 18, 156, 20
        for idx, day in enumerate(stats["week_days"]):
            bx = start_x + idx * slot_w
            cx_b = bx + bar_w // 2
            val = day["total"]
            h = (max(4, int(round((val / max_day) * max_h))) // 2) * 2 if (max_day > 0 and val > 0) else 2
            by = base_y - h
            if day["is_today"]:
                fill, hi, border, lbl_col = "#F8B800", "#FFF090", pal["border"], pal["ink"]
            elif val > 0:
                fill, hi, border, lbl_col = "#3898F8", "#88D0FF", pal["border"], pal["muted"]
            else:
                fill, hi, border, lbl_col = pal["bar_empty"], pal["bar_empty"], pal["muted"], pal["muted"]

            if h >= 4:
                bar_parts.append(
                    f'<rect x="{bx}" y="{by}" width="{bar_w}" height="{h}" fill="{border}"/>'
                    f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="{max(2, h - 4)}" fill="{fill}"/>'
                    f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="2" fill="{hi}"/>'
                )
            else:
                bar_parts.append(f'<rect x="{bx}" y="{by}" width="{bar_w}" height="2" fill="{border}"/>')
            bar_parts.append(render_pixel_text(day["label"], cx_b, 159, 1, lbl_col, align="center"))
        bar_parts_svg = "".join(bar_parts)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  {_render_battle_scene(pal, anim_frame=anim_frame)}
  {_render_battle_status_plate(6, 8, 136, 90, pal, exp_pct=exp_pct, exp_color="#38C0F8")}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 10, 14, 2)}
  {render_pixel_text("GEMINI", 28, 16, 2, pal['ink'])}
  {render_pixel_text("Lv", 122 - pixel_text_width(str(lv_val), 2), 20, 1, pal['blue'])}
  {render_pixel_text(str(lv_val), 134, 16, 2, pal['blue'], align="right")}
  {_render_hp_bar(14, 34, 74, cache_pct, pal, label="HP")}
  {render_pixel_text(f"{cache_pct}%", 134, 34, 2, pal['ink'], align="right")}
  {render_pixel_text(today_total_str, 14, 52, hero_scale, pal['ink'], shadow=pal['plate_shadow'], shadow_offset=2)}

  {_render_stage_pokemon(sp_key, anim_frame, defeated=out_of_tokens)}

  {_render_poke_window(6, 114, 228, 58, pal, accent=accent)}
  {render_pixel_text("EXP 7D", 16, 122, 2, pal['blue'])}
  {render_pixel_text(week_total_str, 224, 122, 2, pal['ink'], align="right")}
  {bar_parts_svg}

  {_render_dialogue_box_themed(6, 175, 228, 61, pal)}
  {render_pixel_text(f"IN:{in_str}", 18, 185, 2, pal['ink']) if not out_of_tokens else render_pixel_text(f"D:{daily_reset}", 18, 185, 2, pal['ink'])}
  {render_pixel_text(f"OUT:{out_str}", 220, 185, 2, pal['ink'], align="right") if not out_of_tokens else render_pixel_text(f"W:{weekly_reset}", 220, 185, 2, pal['ink'], align="right")}
  {render_pixel_text(f"CCH:{cache_str}" if not out_of_tokens else "CUOTA AGOTADA", 18, 203, 2, pal['blue'])}
  {render_pixel_text(updated_hm, 202, 203, 2, pal['muted'], align="right")}
  {render_pixel_text(f"RESET D:{daily_reset}  W:{weekly_reset}", 18, 220, 1, pal['muted']) if not out_of_tokens else ""}
</svg>
'''


# ============================================================
# 2. CLAUDE CODE TOKENS (Pokémon Battle HUD)
# ============================================================
def build_claude_svg(stats: dict, cfg: dict | None = None, anim_frame: int = 0, now: datetime | None = None) -> str:
    """Build a Claude Code rate-limit screen from official status-line data."""
    if now is None:
        now = datetime.now().astimezone()
    pal = resolve_palette(cfg, now)
    sprites = (cfg or {}).get("sprites") or {}
    sp_key, _ = _resolve_species(sprites.get("claude", "charmander"), "charmander")
    rate_limits = stats.get("rate_limits") or {}
    five_hour = rate_limits.get("five_hour")
    seven_day = rate_limits.get("seven_day")

    def usage_percent(window: dict | None) -> float | None:
        value = window.get("used_percentage") if isinstance(window, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            return None
        return float(value)

    five_pct = usage_percent(five_hour)
    seven_pct = usage_percent(seven_day)
    has_real_data = five_pct is not None or seven_pct is not None
    out_of_tokens = any(value is not None and value >= 100 for value in (five_pct, seven_pct))

    five_label = f"{int(five_pct + 0.5)}%" if five_pct is not None else "--%"
    seven_label = f"{int(seven_pct + 0.5)}%" if seven_pct is not None else "--%"
    seven_fill_w = int(round((seven_pct / 100) * 196)) if seven_pct is not None else 0

    def reset_label(window: dict | None) -> str:
        stamp = window.get("resets_at") if isinstance(window, dict) else None
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return "--:--"
        reset_at = datetime.fromtimestamp(stamp).astimezone()
        local_today = now.astimezone().date()
        if reset_at.date() == local_today:
            return reset_at.strftime("%H:%M")
        days = max(0, (reset_at.date() - local_today).days)
        return f"+{days}D {reset_at:%H:%M}"

    five_reset = reset_label(five_hour)
    seven_reset = reset_label(seven_day)
    updated_at = stats.get("quota_updated_at")
    updated_hm = (
        datetime.fromtimestamp(updated_at).astimezone().strftime("%H:%M")
        if has_real_data and isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool)
        else "--:--"
    )
    hero_value = five_label
    hero_scale = 3
    header_status = (
        "REAL" if five_pct is not None and seven_pct is not None
        else "PARCIAL" if has_real_data
        else "SIN DATOS"
    )

    bar_parts_svg = (
        f'<g id="claude-weekly-bar">'
        f'<rect x="20" y="144" width="200" height="10" fill="#202830"/>'
        f'<rect x="22" y="146" width="196" height="6" fill="{pal["bar_empty"]}"/>'
        f'<rect x="22" y="146" width="{seven_fill_w}" height="6" fill="#A855F7"/>'
        f'<rect x="22" y="146" width="{seven_fill_w}" height="2" fill="#D8B4FE"/>'
        f'</g>'
        if seven_pct is not None
        else f'<g id="claude-weekly-empty">{render_pixel_text("SIN DATOS", 120, 144, 2, pal["muted"], align="center")}</g>'
    )

    pokemon_svg = _render_stage_pokemon(sp_key, anim_frame, defeated=out_of_tokens)

    def text_group(group_id: str, svg_text: str) -> str:
        return f'<g id="{group_id}">{svg_text}</g>'

    source_title = (
        f"Uso oficial Claude Code: 5h {five_label}, 7d {seven_label}."
        if has_real_data
        else "Cuota real de Claude Code no disponible; falta recibir datos del status line."
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <title>{source_title}</title>
  {_render_battle_scene(pal, anim_frame=anim_frame)}
  {_render_battle_status_plate(6, 8, 136, 92, pal, exp_pct=seven_pct or 0.0, exp_color="#A855F7")}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 12, 13, 2)}
  {text_group("claude-header-title", render_pixel_text("CLAUDE", 30, 13, 2, pal['ink']))}
  {text_group("claude-header-status", render_pixel_text(header_status, 30, 28, 1, pal['blue']))}

  <g id="claude-pokemon">{pokemon_svg}</g>

  {text_group("claude-primary-label", render_pixel_text("USO 5H", 16, 38, 2, pal['muted']))}
  {text_group("claude-primary-value", render_pixel_text(hero_value, 16, 53, hero_scale, pal['ink'], shadow=pal['plate_shadow'], shadow_offset=2))}
  {_render_hp_bar(14, 75, 120, five_pct, pal, label="5H", invert_danger=True)}

  {_render_poke_window(6, 114, 228, 52, pal, accent="#A855F7")}
  {text_group("claude-weekly-title", render_pixel_text("USO 7D", 16, 122, 2, pal['blue']))}
  {text_group("claude-weekly-value", render_pixel_text(seven_label, 224, 122, 2, pal['ink'], align="right"))}
  {bar_parts_svg}

  {_render_dialogue_box_themed(6, 170, 228, 66, pal)}
  {text_group("claude-reset-5h-label", render_pixel_text("5H RESET", 18, 181, 2, pal['ink']))}
  {text_group("claude-reset-5h-value", render_pixel_text(five_reset, 220, 181, 2, pal['ink'], align="right"))}
  {text_group("claude-reset-7d-label", render_pixel_text("7D RESET", 18, 198, 2, pal['ink']))}
  {text_group("claude-reset-7d-value", render_pixel_text(seven_reset, 220, 198, 2, pal['ink'], align="right"))}
  {text_group("claude-statusline-hint", render_pixel_text(f"REAL {updated_hm}" if has_real_data else "CONFIGURA STATUS LINE", 18, 217, 1, pal['muted']))}
</svg>
'''


# ============================================================
# 3. POKÉGEAR CLOCK (Authentic Day/Night Pokémon Scene + Digital Clock)
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

    header_title = truncate_pixel_text(str(device_cfg.get("trainer_name") or "POKEGEAR"), 98, 2) or "POKEGEAR"
    city = truncate_pixel_text(str(device_cfg.get("city") or "PARACAS"), 100, 2)
    temp_str = ""
    cond_short = ""
    if weather and weather.get("temp") is not None:
        t = weather["temp"]
        if not celsius:
            t = t * 9 / 5 + 32
        unit = "C" if celsius else "F"
        temp_str = f"{int(round(t))}°{unit}"
        avail_cond_px = max(68, 174 - pixel_text_width(city, 2))
        cond_short = truncate_pixel_text(str(weather.get("description") or ""), avail_cond_px, 2)

    if is_night:
        sp_key, _ = _resolve_species(sprites.get("clock_night", "gengar"), "gengar")
        badge_bg = "#483880"
        badge_txt = "NOCHE"
        badge_w = 66
        accent = "#7858A8"
    else:
        sp_key, _ = _resolve_species(sprites.get("clock_day", "bulbasaur"), "bulbasaur")
        badge_bg = "#249440"
        badge_txt = "DIA"
        badge_w = 46
        accent = "#249440"

    hud_stat = temp_str or f"{now.day:02d}/{now.month:02d}"
    clock_x = 18 if ampm else 40
    ampm_svg = ""
    if ampm:
        ampm_svg = (
            f'<rect x="184" y="136" width="38" height="22" fill="{pal["border"]}"/>'
            f'<rect x="186" y="138" width="34" height="18" fill="{badge_bg}"/>'
            f'{render_pixel_text(ampm, 203, 141, 2, "#FFFFFF", align="center")}'
        )

    bottom_right = cond_short if (cond_short and temp_str) else temp_str
    min_pct = (now.minute / 59.0) * 100.0
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  {_render_battle_scene(pal, anim_frame=anim_frame, night_override=is_night)}
  {_render_battle_status_plate(6, 8, 136, 88, pal, exp_pct=min_pct, exp_color=accent)}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 12, 14, 2)}
  {render_pixel_text(header_title, 32, 16, 2, pal['ink'])}
  <rect x="14" y="34" width="{badge_w}" height="16" fill="{badge_bg}"/>
  {render_pixel_text(badge_txt, 14 + badge_w // 2, 36, 2, "#FFFFFF", align="center")}
  {render_signal_bars(122, 36, accent)}
  {render_pixel_text(hud_stat, 14, 56, 3, pal['ink'], shadow=pal['plate_shadow'], shadow_offset=2)}

  {_render_stage_pokemon(sp_key, anim_frame)}

  {_render_poke_window(6, 114, 228, 60, pal, accent=accent)}
  {render_pixel_text(time_str, clock_x, 126, 6, pal['ink'], shadow=pal['card_shadow'], shadow_offset=2)}
  {ampm_svg}

  {_render_dialogue_box_themed(6, 178, 228, 58, pal)}
  {render_pixel_text(date_str, 18, 189, 2, pal['ink'])}
  {render_pixel_text(city, 18, 209, 2, pal['blue'])}
  {render_pixel_text(bottom_right, 202, 209, 2, pal['muted'], align="right")}
</svg>
'''


# ============================================================
# 4. POKÉ WEATHER (Dynamic Weather Arena + 3-Day Forecast)
# ============================================================
def _weather_species(main: str, override: str = "auto") -> tuple[str, str]:
    if override and override != "auto" and override.lower() in OFFICIAL_SPRITES:
        return _resolve_species(override, "snorlax")
    m = (main or "").lower()
    if any(k in m for k in ("rain", "drizzle", "thunder", "storm")):
        return "squirtle", "#3898F8"
    if "clear" in m or "sun" in m:
        return "charmander", "#F87828"
    return "snorlax", "#387888"


def _mini_weather_icon(main: str, x: int, y: int) -> str:
    m = (main or "").lower()
    if any(k in m for k in ("thunder", "storm")):
        return render_sprite(MINI_STORM, MINI_STORM_PALETTE, x, y, 2)
    if any(k in m for k in ("rain", "drizzle")):
        return render_sprite(MINI_RAIN, MINI_RAIN_PAL, x, y, 2)
    if "clear" in m or "sun" in m:
        return render_sprite(MINI_SUN, MINI_SUN_PAL, x, y, 2)
    return render_sprite(MINI_CLOUD, MINI_CLOUD_PAL, x, y, 2)


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

    city = truncate_pixel_text(str(weather.get("city") or device_cfg.get("city") or "PARACAS"), 98, 2)
    temp_val = conv(float(weather.get("temp", 20)))
    temp_str = f"{temp_val}°{unit}"
    hum = int(weather.get("humidity", 0))
    wind_kmh = int(round(float(weather.get("wind_ms", 0)) * 3.6))
    desc = truncate_pixel_text(str(weather.get("description") or "CLIMA"), 192, 2)
    main = str(weather.get("main") or "Clouds")
    updated_hm = str(weather.get("updated_hm") or "")

    sp_key, accent = _weather_species(main, str(sprites.get("weather", "auto")))

    fc_parts: list[str] = []
    days = (weather.get("forecast") or [])[:3]
    col_w = 72
    for idx, d in enumerate(days):
        cx_f = 12 + idx * col_w + col_w // 2
        if idx > 0:
            vx = 12 + idx * col_w
            fc_parts.append(f'<rect x="{vx}" y="122" width="2" height="44" fill="{pal["divider"]}"/>')
        lbl = sanitize_pixel_text(d.get("label", "---"))[:3]
        fc_parts.append(render_pixel_text(lbl, cx_f, 121, 2, pal["blue"], align="center"))
        fc_parts.append(_mini_weather_icon(d.get("main", ""), cx_f - 7, 136))
        tmax = conv(float(d.get("max", temp_val)))
        tmin = conv(float(d.get("min", temp_val)))
        hi_txt = f"{tmax}"
        lo_txt = f"/{tmin}"
        total_w = pixel_text_width(hi_txt, 2, gap=1) + pixel_text_width(lo_txt, 2, gap=1)
        tx = cx_f - total_w // 2
        fc_parts.append(render_pixel_text(hi_txt, tx, 154, 2, pal["ink"], gap=1))
        fc_parts.append(render_pixel_text(lo_txt, tx + pixel_text_width(hi_txt, 2, gap=1), 154, 2, pal["muted"], gap=1))

    if not fc_parts:
        fc_parts.append(render_pixel_text("SIN PRONOSTICO", 120, 138, 2, pal["muted"], align="center"))

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  {_render_battle_scene(pal, anim_frame=anim_frame, weather_mode=main)}
  {_render_battle_status_plate(6, 8, 136, 88, pal, exp_pct=float(hum), exp_color="#3898F8")}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 12, 14, 2)}
  {render_pixel_text(city, 32, 16, 2, pal['ink'])}
  {_render_hp_bar(14, 34, 74, hum, pal, label="HM")}
  {render_pixel_text(f"{hum}%", 134, 34, 2, pal['ink'], align="right")}
  {render_pixel_text(temp_str, 14, 52, 4, pal['ink'], shadow=pal['plate_shadow'], shadow_offset=2)}
  {_mini_weather_icon(main, 118, 58)}

  {_render_stage_pokemon(sp_key, anim_frame)}

  {_render_poke_window(6, 114, 228, 60, pal, accent=accent)}
  {"".join(fc_parts)}

  {_render_dialogue_box_themed(6, 178, 228, 58, pal)}
  {render_pixel_text(desc, 18, 189, 2, pal['ink'])}
  {render_pixel_text(f"VNT {wind_kmh}KMH", 18, 209, 2, pal['blue'])}
  {render_pixel_text(updated_hm, 202, 209, 2, pal['muted'], align="right")}
</svg>
'''


# ============================================================
# 5. SPOTIFY NOW PLAYING (Pokégear Radio / Jigglypuff Stage)
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
    sp_key, _ = _resolve_species(sprites.get("spotify", "jigglypuff"), "jigglypuff")

    f6 = anim_frame % len(_SPOTIFY_EQ_FRAMES)
    note_l_dy = -2 if f6 in (1, 2, 4) else 0
    note_r_dy = 0 if f6 in (1, 2, 4) else -2

    title = str(track.get("title") or "SIN TITULO")
    artist = str(track.get("artist") or "ARTISTA")
    album = str(track.get("album") or "")

    title_lines = wrap_pixel_text(title, 196, 2, max_lines=2)
    artist_line = truncate_pixel_text(artist, 196, 2)
    album_line = truncate_pixel_text(album, 168, 2) if album else "EN REPRODUCCION"

    eq_heights = _SPOTIFY_EQ_FRAMES[f6]
    bgm_pct = _SPOTIFY_BGM_PCTS[f6]
    eq_parts = []
    for idx, h in enumerate(eq_heights):
        ex = 16 + idx * 13
        ey = 84 - h
        mid_h = max(2, h - 4)
        eq_parts.append(
            f'<rect x="{ex}" y="{ey}" width="10" height="{h}" fill="{pal["border"]}"/>'
            f'<rect x="{ex + 2}" y="{ey + 2}" width="6" height="{mid_h}" fill="#1DB954"/>'
            f'<rect x="{ex + 2}" y="{ey + 2}" width="6" height="2" fill="#88F8A8"/>'
        )

    title_svg = render_pixel_text(title_lines[0], 18, 128, 2, pal["ink"])
    if len(title_lines) > 1:
        title_svg += render_pixel_text(title_lines[1], 18, 146, 2, pal["ink"])
        div_y, artist_y, album_y = 166, 176, 198
    else:
        div_y, artist_y, album_y = 152, 166, 192

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  {_render_battle_scene(pal, anim_frame=anim_frame)}
  {_render_battle_status_plate(6, 8, 136, 88, pal, exp_pct=float(bgm_pct), exp_color="#1DB954")}
  {render_sprite(NOTE_SPRITE, NOTE_PALETTE, 12, 13 + note_l_dy, 2)}
  {render_pixel_text("SPOTIFY", 28, 16, 2, pal['ink'], gap=1)}
  <rect x="108" y="14" width="28" height="14" fill="#1DB954"/>
  {render_pixel_text("ON", 122, 18, 1, "#FFFFFF", align="center")}
  {_render_hp_bar(14, 34, 120, bgm_pct, pal, label="FM")}
  {"".join(eq_parts)}

  {_render_stage_pokemon(sp_key, anim_frame, bob_seq=_STAGE_BOB_6)}
  {render_sprite(NOTE_SPRITE, NOTE_PALETTE, 220, 10 + note_r_dy, 2)}

  {_render_dialogue_box_themed(6, 114, 228, 122, pal)}
  {title_svg}
  {render_dotted_rule(18, div_y, 204, pal['divider'])}
  {render_pixel_text(artist_line, 18, artist_y, 2, pal['green'])}
  {render_pixel_text(album_line, 18, album_y, 2, pal['muted'])}
</svg>
'''


# ============================================================
# 6. POKÉMON NOTIFICATION CARD (Wild Encounter Alert)
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
    sp_key, accent = _resolve_species(pokemon, "pikachu")
    banner_text = truncate_pixel_text(app_name, 94, 2) if app_name else ""
    if not banner_text:
        banner_text = "AVISO!"
    title_clean = truncate_pixel_text(title or "AVISO!", 118, 2)
    msg_lines = wrap_pixel_text(message or "", 198, 2, max_lines=3)
    now_hm = now.strftime("%H:%M")

    f4 = anim_frame % 4
    sparkle_svg = (
        render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 222, 10 - (2 if f4 % 2 else 0), 2)
        if f4 in (0, 2)
        else _render_twinkle(224, 12, "#F8D030", alt=True)
    )

    msg_svg = ""
    for idx, line in enumerate(msg_lines):
        msg_svg += render_pixel_text(line, 18, 128 + idx * 24, 2, pal["ink"])

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  {_render_battle_scene(pal, anim_frame=anim_frame)}
  {_render_battle_status_plate(6, 8, 136, 88, pal, exp_pct=100.0, exp_color=accent)}
  <rect x="12" y="14" width="16" height="16" fill="{accent}"/>
  {render_pixel_text("!", 20, 16, 2, "#FFFFFF", align="center")}
  <rect x="30" y="14" width="104" height="16" fill="{pal['border']}"/>
  {render_pixel_text(banner_text, 82, 16, 2, "#F8D030", align="center")}

  {render_pixel_text(title_clean, 14, 42, 2, pal['ink'])}
  {render_pixel_text(now_hm, 14, 64, 2, pal['muted'])}

  {_render_stage_pokemon(sp_key, anim_frame)}
  {sparkle_svg}

  {_render_dialogue_box_themed(6, 114, 228, 122, pal)}
  {msg_svg}
</svg>
'''
