"""
All 5 Pokémon Game Boy Color (Gold/Silver/Crystal) 240x240 screens with
full PC-side personalization (palettes, sprites per screen, trainer name, units):
  1. gemini   — Battle HUD (Antigravity Gemini Token Usage)
  2. clock    — PokéGear Clock (Day / Night sprites)
  3. weather  — Weather + 3-Day Forecast
  4. spotify  — Concert Stage (Spotify Now Playing, exclusive auto)
  5. notify   — CLI / API Instant Alert
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
    render_battle_pad,
    render_gbc_dialogue_box,
    render_pixel_text,
    render_sprite,
    sanitize_pixel_text,
    truncate_pixel_text,
    wrap_pixel_text,
)

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

PALETTES: dict[str, dict[str, str]] = {
    "gbc_classic": {
        "bg": "#F8F8F0",
        "card": "#FFFFFF",
        "ink": "#181820",
        "border": "#181820",
        "muted": "#606870",
        "shadow": "#C8D0C0",
        "divider": "#D0D8C8",
        "blue": "#2868C8",
        "green": "#15803D",
        "top_band": "#E8F0D8",
        "pad_top": "#98C078",
        "pad_mid": "#C8E4A8",
        "pad_bot": "#78A058",
        "bar_empty": "#D0D8C8",
    },
    "gbc_dark": {
        "bg": "#101422",
        "card": "#181F34",
        "ink": "#F0F4FF",
        "border": "#7A8CB8",
        "muted": "#8C9AB8",
        "shadow": "#070A12",
        "divider": "#2C3858",
        "blue": "#60A5FA",
        "green": "#34D399",
        "top_band": "#242F4D",
        "pad_top": "#2A395A",
        "pad_mid": "#394D78",
        "pad_bot": "#1E2942",
        "bar_empty": "#2B3550",
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
    },
    "pokedex_red": {
        "bg": "#FFF4F0",
        "card": "#FFFFFF",
        "ink": "#1C1418",
        "border": "#1C1418",
        "muted": "#785860",
        "shadow": "#E0B8B0",
        "divider": "#E8C8C0",
        "blue": "#C82828",
        "green": "#15803D",
        "top_band": "#E03838",
        "pad_top": "#D89890",
        "pad_mid": "#F4C4BC",
        "pad_bot": "#B06860",
        "bar_empty": "#E8D0C8",
    },
}


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


def _get_sprite(name: str, fallback: str = "pikachu") -> tuple[list[str], dict[str, str], str]:
    return SPRITE_CATALOG.get((name or "").lower(), SPRITE_CATALOG[fallback])


def _render_dialogue_box_themed(x: int, y: int, w: int, h: int, pal: dict[str, str], cursor: bool = True) -> str:
    b = pal["border"]
    bg = pal["bg"]
    parts = [
        f'<rect x="{x + 2}" y="{y}" width="{w - 4}" height="{h}" fill="{b}"/>',
        f'<rect x="{x}" y="{y + 2}" width="{w}" height="{h - 4}" fill="{b}"/>',
        f'<rect x="{x + 4}" y="{y + 4}" width="{w - 8}" height="{h - 8}" fill="{bg}"/>',
        f'<rect x="{x + 6}" y="{y + 6}" width="{w - 12}" height="{h - 12}" fill="{b}"/>',
        f'<rect x="{x + 8}" y="{y + 8}" width="{w - 16}" height="{h - 16}" fill="{bg}"/>',
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
        cy = y + h - 20
        parts.extend([
            f'<rect x="{cx}" y="{cy}" width="10" height="2" fill="#E83838"/>',
            f'<rect x="{cx + 2}" y="{cy + 2}" width="6" height="2" fill="#E83838"/>',
            f'<rect x="{cx + 4}" y="{cy + 4}" width="2" height="2" fill="#E83838"/>',
        ])
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
# 1. GEMINI TOKENS (Battle HUD)
# ============================================================
def build_gemini_svg(stats: dict, cfg: dict | None = None) -> str:
    pal = resolve_palette(cfg, datetime.now().astimezone())
    sprites = (cfg or {}).get("sprites") or {}
    sp_grid, sp_pal, _ = _get_sprite(sprites.get("gemini", "pikachu"), "pikachu")

    today_total_str = fmt_tokens(stats["today_total"])
    in_str = fmt_tokens(stats["today_in"])
    out_str = fmt_tokens(stats["today_out"])
    cache_str = fmt_tokens(stats["today_cache"])
    week_total_str = fmt_tokens(stats["week_total"])
    updated_hm = stats["updated_hm"]
    steps = stats["today_steps"]
    lv_val = min(99, max(1, steps)) if steps > 0 else 1
    cache_pct = stats["cache_pct"]

    if cache_pct >= 50:
        hp_color, hp_hi = "#30B848", "#78E878"
    elif cache_pct >= 20:
        hp_color, hp_hi = "#F8C828", "#FFF080"
    else:
        hp_color, hp_hi = "#E83838", "#F88888"

    hp_max_w = 72
    hp_fill_w = (max(2, int(round((cache_pct / 100.0) * hp_max_w))) // 2) * 2 if cache_pct > 0 else 0
    hero_scale = 4 if pixel_text_width(today_total_str, 4) <= 126 else 3

    max_day = max((d["total"] for d in stats["week_days"]), default=0)
    bar_parts: list[str] = []
    start_x, slot_w, bar_w, base_y, max_h = 20, 29, 20, 148, 20

    for idx, day in enumerate(stats["week_days"]):
        bx = start_x + idx * slot_w
        cx = bx + bar_w // 2
        val = day["total"]
        if max_day > 0 and val > 0:
            h = (max(4, int(round((val / max_day) * max_h))) // 2) * 2
        else:
            h = 2
        by = base_y - h
        if day["is_today"]:
            fill, hi, border, lbl_col = "#F8B800", "#FFE878", pal["border"], pal["ink"]
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
        bar_parts.append(render_pixel_text(day["label"], cx, 152, 2, lbl_col, align="center"))

    hero_y = 50 if hero_scale == 4 else 54
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="6" fill="{pal['top_band']}"/>
  {render_battle_pad(194, 76, pal['pad_top'], pal['pad_mid'], pal['pad_bot'])}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_sprite(sp_grid, sp_pal, 158, 16, 4)}
  {render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 218, 10, 2)}
  {render_pixel_text("GEMINI", 28, 12, 2, pal['ink'])}
  {render_pixel_text(f"Lv{lv_val}", 152, 12, 2, pal['ink'], align="right")}
  <rect x="12" y="30" width="22" height="12" fill="{pal['border']}"/>
  {render_pixel_text("HP", 16, 33, 1, "#F8C828")}
  <rect x="34" y="30" width="{hp_max_w + 4}" height="12" fill="{pal['border']}"/>
  <rect x="36" y="32" width="{hp_max_w}" height="8" fill="{pal['bg']}"/>
  <rect x="36" y="32" width="{hp_fill_w}" height="8" fill="{hp_color}"/>
  <rect x="36" y="32" width="{hp_fill_w}" height="2" fill="{hp_hi}"/>
  {render_pixel_text(f"{cache_pct}%", 152, 30, 2, pal['ink'], align="right")}
  {render_pixel_text(today_total_str, 18, hero_y, hero_scale, pal['ink'], shadow=pal['shadow'])}
  <rect x="8" y="28" width="4" height="62" fill="{pal['border']}"/>
  <rect x="8" y="88" width="138" height="4" fill="{pal['border']}"/>
  <rect x="146" y="86" width="4" height="6" fill="{pal['border']}"/>
  <rect x="150" y="90" width="4" height="2" fill="{pal['border']}"/>
  <rect x="12" y="92" width="132" height="2" fill="#3898F8"/>
  <rect x="10" y="104" width="224" height="64" fill="{pal['shadow']}"/>
  <rect x="8" y="102" width="224" height="64" fill="{pal['border']}"/>
  <rect x="10" y="104" width="220" height="60" fill="{pal['card']}"/>
  {render_pixel_text("EXP 7D", 16, 110, 2, pal['blue'])}
  {render_pixel_text(week_total_str, 224, 110, 2, pal['ink'], align="right")}
  {"".join(bar_parts)}
  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(f"IN:{in_str}", 22, 186, 2, pal['ink'])}
  {render_pixel_text(f"OUT:{out_str}", 218, 186, 2, pal['ink'], align="right")}
  {render_pixel_text(f"CCH:{cache_str}", 22, 206, 2, pal['ink'])}
  {render_pixel_text(updated_hm, 202, 206, 2, pal['muted'], align="right")}
</svg>
'''


# ============================================================
# 2. POKÉGEAR CLOCK (Day / Night Sprite + Trainer Name)
# ============================================================
def build_clock_svg(now: datetime, device_cfg: dict, weather: dict | None) -> str:
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

    header_title = truncate_pixel_text(str(device_cfg.get("trainer_name") or "POKEGEAR"), 100, 2) or "POKEGEAR"
    city = truncate_pixel_text(str(device_cfg.get("city") or "PARACAS"), 115, 2)
    temp_str = ""
    if weather and weather.get("temp") is not None:
        t = weather["temp"]
        if not celsius:
            t = t * 9 / 5 + 32
        unit = "C" if celsius else "F"
        temp_str = f"{int(round(t))}°{unit}"

    if is_night:
        sp_grid, sp_pal, _ = _get_sprite(sprites.get("clock_night", "gengar"), "gengar")
        badge_bg = "#483878"
        badge_txt = "NOCHE"
        sky_icon = render_sprite(MINI_MOON, MINI_MOON_PAL, 134, 12, 2)
        accent = "#7858A8"
    else:
        sp_grid, sp_pal, _ = _get_sprite(sprites.get("clock_day", "bulbasaur"), "bulbasaur")
        badge_bg = "#30A848"
        badge_txt = "DIA"
        sky_icon = render_sprite(MINI_SUN, MINI_SUN_PAL, 132, 10, 2)
        accent = "#30A848"

    clock_x = 18 if ampm else 42
    ampm_svg = ""
    if ampm:
        ampm_svg = (
            f'<rect x="184" y="132" width="38" height="20" fill="{badge_bg}"/>'
            f'{render_pixel_text(ampm, 203, 136, 2, "#FFFFFF", align="center")}'
        )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="6" fill="{accent}"/>

  {render_battle_pad(194, 78, pal['pad_top'], pal['pad_mid'], pal['pad_bot'])}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_pixel_text(header_title, 28, 12, 2, pal['ink'])}
  {sky_icon}
  {render_sprite(sp_grid, sp_pal, 158, 16, 4)}

  <rect x="16" y="34" width="70" height="18" fill="{badge_bg}"/>
  {render_pixel_text(badge_txt, 51, 37, 2, "#FFFFFF", align="center")}
  {render_pixel_text(f"{now.day:02d}/{now.month:02d}", 18, 60, 3, pal['ink'], shadow=pal['shadow'])}

  <rect x="8" y="32" width="4" height="58" fill="{pal['border']}"/>
  <rect x="8" y="88" width="138" height="4" fill="{pal['border']}"/>
  <rect x="146" y="86" width="4" height="6" fill="{pal['border']}"/>
  <rect x="12" y="92" width="132" height="2" fill="{accent}"/>

  <rect x="10" y="102" width="224" height="64" fill="{pal['shadow']}"/>
  <rect x="8" y="100" width="224" height="64" fill="{pal['border']}"/>
  <rect x="10" y="102" width="220" height="60" fill="{pal['card']}"/>
  <rect x="10" y="102" width="220" height="4" fill="{accent}"/>
  {render_pixel_text(time_str, clock_x, 116, 6, pal['ink'], shadow=pal['shadow'])}
  {ampm_svg}

  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(date_str, 22, 186, 2, pal['ink'])}
  {render_pixel_text(city, 22, 206, 2, pal['blue'])}
  {render_pixel_text(temp_str, 202, 206, 2, pal['ink'], align="right")}
</svg>
'''


# ============================================================
# 3. POKÉ WEATHER (Dynamic or Custom Sprite + 3-Day Forecast)
# ============================================================
def _weather_pokemon(main: str, override: str = "auto") -> tuple[list[str], dict[str, str], str]:
    if override and override != "auto" and override in SPRITE_CATALOG:
        return SPRITE_CATALOG[override]
    m = (main or "").lower()
    if any(k in m for k in ("rain", "drizzle", "thunder", "storm")):
        return SQUIRTLE_SPRITE, SQUIRTLE_PALETTE, "#3898F8"
    if "clear" in m or "sun" in m:
        return CHARMANDER_SPRITE, CHARMANDER_PALETTE, "#F87828"
    return SNORLAX_SPRITE, SNORLAX_PALETTE, "#387888"


def _mini_weather_icon(main: str, x: int, y: int) -> str:
    m = (main or "").lower()
    if any(k in m for k in ("rain", "drizzle", "thunder", "storm")):
        return render_sprite(MINI_RAIN, MINI_RAIN_PAL, x, y, 2)
    if "clear" in m or "sun" in m:
        return render_sprite(MINI_SUN, MINI_SUN_PAL, x, y, 2)
    return render_sprite(MINI_CLOUD, MINI_CLOUD_PAL, x, y, 2)


def build_weather_svg(weather: dict, device_cfg: dict) -> str:
    pal = resolve_palette(device_cfg, datetime.now().astimezone())
    sprites = device_cfg.get("sprites") or {}
    celsius = bool(device_cfg.get("celsius", True))
    unit = "C" if celsius else "F"

    def conv(t: float) -> int:
        return int(round(t if celsius else t * 9 / 5 + 32))

    city = truncate_pixel_text(str(weather.get("city") or device_cfg.get("city") or "PARACAS"), 118, 2)
    temp_val = conv(float(weather.get("temp", 20)))
    temp_str = f"{temp_val}°{unit}"
    hum = int(weather.get("humidity", 0))
    wind_kmh = int(round(float(weather.get("wind_ms", 0)) * 3.6))
    desc = truncate_pixel_text(str(weather.get("description") or "CLIMA"), 192, 2)
    main = str(weather.get("main") or "Clouds")
    updated_hm = str(weather.get("updated_hm") or "")

    sprite_grid, sprite_pal, accent = _weather_pokemon(main, str(sprites.get("weather", "auto")))

    hum_max_w = 68
    hum_fill_w = (max(2, int(round((min(100, max(0, hum)) / 100.0) * hum_max_w))) // 2) * 2 if hum > 0 else 0

    fc_parts: list[str] = []
    days = (weather.get("forecast") or [])[:3]
    col_w = 72
    for idx, d in enumerate(days):
        cx = 12 + idx * col_w + col_w // 2
        if idx > 0:
            vx = 12 + idx * col_w
            fc_parts.append(f'<rect x="{vx}" y="108" width="2" height="52" fill="{pal["divider"]}"/>')
        lbl = sanitize_pixel_text(d.get("label", "---"))[:3]
        fc_parts.append(render_pixel_text(lbl, cx, 108, 2, pal["blue"], align="center"))
        fc_parts.append(_mini_weather_icon(d.get("main", ""), cx - 7, 124))
        tmax = conv(float(d.get("max", temp_val)))
        tmin = conv(float(d.get("min", temp_val)))
        fc_parts.append(render_pixel_text(f"{tmax}/{tmin}", cx, 146, 2, pal["ink"], align="center", gap=1))

    if not fc_parts:
        fc_parts.append(render_pixel_text("SIN PRONOSTICO", 120, 128, 2, pal["muted"], align="center"))

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="6" fill="{accent}"/>

  {render_battle_pad(194, 76, pal['pad_top'], pal['pad_mid'], pal['pad_bot'])}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)}
  {render_pixel_text(city, 28, 12, 2, pal['ink'])}
  {render_sprite(sprite_grid, sprite_pal, 158, 16, 4)}

  <rect x="12" y="30" width="26" height="12" fill="{pal['border']}"/>
  {render_pixel_text("HUM", 15, 33, 1, "#68D0FF")}
  <rect x="38" y="30" width="{hum_max_w + 4}" height="12" fill="{pal['border']}"/>
  <rect x="40" y="32" width="{hum_max_w}" height="8" fill="{pal['bg']}"/>
  <rect x="40" y="32" width="{hum_fill_w}" height="8" fill="#3898F8"/>
  <rect x="40" y="32" width="{hum_fill_w}" height="2" fill="#88D8FF"/>
  {render_pixel_text(f"{hum}%", 152, 30, 2, pal['ink'], align="right")}

  {render_pixel_text(temp_str, 18, 50, 4, pal['ink'], shadow=pal['shadow'])}

  <rect x="8" y="28" width="4" height="62" fill="{pal['border']}"/>
  <rect x="8" y="88" width="138" height="4" fill="{pal['border']}"/>
  <rect x="146" y="86" width="4" height="6" fill="{pal['border']}"/>
  <rect x="12" y="92" width="132" height="2" fill="{accent}"/>

  <rect x="10" y="104" width="224" height="64" fill="{pal['shadow']}"/>
  <rect x="8" y="102" width="224" height="64" fill="{pal['border']}"/>
  <rect x="10" y="104" width="220" height="60" fill="{pal['card']}"/>
  {"".join(fc_parts)}

  {_render_dialogue_box_themed(8, 172, 224, 62, pal)}
  {render_pixel_text(desc, 22, 186, 2, pal['ink'])}
  {render_pixel_text(f"VNT:{wind_kmh}KMH", 22, 206, 2, pal['blue'])}
  {render_pixel_text(updated_hm, 202, 206, 2, pal['muted'], align="right")}
</svg>
'''


# ============================================================
# 4. SPOTIFY NOW PLAYING (Concert Stage — Exclusive Auto)
# ============================================================
def build_spotify_svg(track: dict, cfg: dict | None = None) -> str:
    pal = resolve_palette(cfg, datetime.now().astimezone())
    sprites = (cfg or {}).get("sprites") or {}
    sp_grid, sp_pal, _ = _get_sprite(sprites.get("spotify", "jigglypuff"), "jigglypuff")

    title = str(track.get("title") or "SIN TITULO")
    artist = str(track.get("artist") or "ARTISTA")
    album = str(track.get("album") or "")

    title_lines = wrap_pixel_text(title, 194, 2, max_lines=2)
    artist_line = truncate_pixel_text(artist, 194, 2)
    album_line = truncate_pixel_text(album, 170, 2) if album else "EN REPRODUCCION"

    eq_heights = [8, 16, 12, 20, 14, 18, 10, 16, 12, 8]
    eq_parts = []
    for idx, h in enumerate(eq_heights):
        ex = 18 + idx * 12
        ey = 82 - h
        eq_parts.append(
            f'<rect x="{ex}" y="{ey}" width="8" height="{h}" fill="{pal["border"]}"/>'
            f'<rect x="{ex + 2}" y="{ey + 2}" width="4" height="{max(2, h - 4)}" fill="#1DB954"/>'
            f'<rect x="{ex + 2}" y="{ey + 2}" width="4" height="2" fill="#78F098"/>'
        )

    title_svg = render_pixel_text(title_lines[0], 22, 122, 2, pal["ink"])
    if len(title_lines) > 1:
        title_svg += render_pixel_text(title_lines[1], 22, 142, 2, pal["ink"])
        div_y, artist_y, album_y = 162, 172, 196
    else:
        div_y, artist_y, album_y = 148, 160, 188

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="6" fill="#1DB954"/>

  {render_battle_pad(194, 78, pal['pad_top'], pal['pad_mid'], pal['pad_bot'])}
  {render_sprite(NOTE_SPRITE, NOTE_PALETTE, 8, 10, 2)}
  {render_pixel_text("SPOTIFY", 26, 12, 2, pal['ink'])}
  <rect x="104" y="10" width="52" height="16" fill="#1DB954"/>
  {render_pixel_text("PLAY", 130, 12, 2, "#FFFFFF", align="center")}

  {render_sprite(sp_grid, sp_pal, 156, 18, 4)}
  {render_sprite(NOTE_SPRITE, NOTE_PALETTE, 220, 8, 2)}

  <rect x="12" y="34" width="24" height="12" fill="{pal['border']}"/>
  {render_pixel_text("BGM", 15, 37, 1, "#78F098")}
  <rect x="36" y="34" width="112" height="12" fill="{pal['border']}"/>
  <rect x="38" y="36" width="108" height="8" fill="{pal['bg']}"/>
  <rect x="38" y="36" width="82" height="8" fill="#1DB954"/>
  <rect x="38" y="36" width="82" height="2" fill="#88F8A8"/>

  {"".join(eq_parts)}

  <rect x="8" y="32" width="4" height="58" fill="{pal['border']}"/>
  <rect x="8" y="88" width="138" height="4" fill="{pal['border']}"/>
  <rect x="146" y="86" width="4" height="6" fill="{pal['border']}"/>
  <rect x="12" y="92" width="132" height="2" fill="#1DB954"/>

  {_render_dialogue_box_themed(8, 104, 224, 130, pal)}
  {title_svg}
  <rect x="22" y="{div_y}" width="196" height="2" fill="{pal['divider']}"/>
  {render_pixel_text(artist_line, 22, artist_y, 2, pal['green'])}
  {render_pixel_text(album_line, 22, album_y, 2, pal['muted'])}
</svg>
'''


# ============================================================
# 5. POKÉMON NOTIFICATION CARD (CLI / API Instant Alert)
# ============================================================
def build_notify_svg(title: str, message: str, pokemon: str = "pikachu", cfg: dict | None = None) -> str:
    pal = resolve_palette(cfg, datetime.now().astimezone())
    sprite_grid, sprite_pal, accent = _get_sprite(pokemon, "pikachu")
    title_clean = truncate_pixel_text(title or "AVISO!", 116, 2)
    msg_lines = wrap_pixel_text(message or "", 194, 2, max_lines=3)
    now_hm = datetime.now().astimezone().strftime("%H:%M")

    msg_svg = ""
    for idx, line in enumerate(msg_lines):
        msg_svg += render_pixel_text(line, 22, 124 + idx * 24, 2, pal["ink"])

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <rect width="240" height="240" fill="{pal['bg']}"/>
  <rect x="0" y="0" width="240" height="8" fill="{accent}"/>

  {render_battle_pad(194, 78, pal['pad_top'], pal['pad_mid'], pal['pad_bot'])}
  {render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 12, 2)}
  <rect x="28" y="12" width="120" height="18" fill="{pal['border']}"/>
  {render_pixel_text("AVISO!", 88, 15, 2, "#F8D030", align="center")}
  {render_sprite(sprite_grid, sprite_pal, 158, 16, 4)}
  {render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 218, 12, 2)}

  {render_pixel_text(title_clean, 16, 44, 2, pal['ink'])}
  {render_pixel_text(now_hm, 16, 68, 2, pal['muted'])}

  <rect x="8" y="34" width="4" height="56" fill="{pal['border']}"/>
  <rect x="8" y="88" width="138" height="4" fill="{pal['border']}"/>
  <rect x="146" y="86" width="4" height="6" fill="{pal['border']}"/>
  <rect x="12" y="92" width="132" height="2" fill="{accent}"/>

  {_render_dialogue_box_themed(8, 104, 224, 130, pal)}
  {msg_svg}
</svg>
'''
