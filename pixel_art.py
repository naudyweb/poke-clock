"""
Authentic Game Boy Color (Pokémon Gold / Silver / Crystal) pixel-art engine.
Pure SVG crispEdges renderer with custom bitmap pixel font and 18x18 sprites.
"""

from __future__ import annotations

import unicodedata

# Crisp 5x6 authentic Game Boy pixel font (with Ñ, °, punctuation)
PIXEL_FONT: dict[str, list[str]] = {
    "0": ["01110", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00110", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010"],
    "5": ["11111", "10000", "11110", "00001", "10001", "01110"],
    "6": ["01110", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000"],
    "8": ["01110", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001"],
    "B": ["11110", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["111", "010", "010", "010", "010", "111"],
    "J": ["00111", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "11100", "10010", "10001", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001"],
    "Ñ": ["01110", "00000", "11001", "10101", "10011", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "11110", "10010", "10001", "10001"],
    "S": ["01111", "10000", "01110", "00001", "10001", "01110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "11011", "10001"],
    "X": ["10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100"],
    "Z": ["11111", "00010", "00100", "01000", "10000", "11111"],
    "v": ["0000", "0000", "1001", "1001", "0110", "0110"],
    ".": ["00", "00", "00", "00", "11", "11"],
    ",": ["00", "00", "00", "00", "01", "10"],
    ":": ["00", "11", "00", "00", "11", "00"],
    "!": ["11", "11", "11", "11", "00", "11"],
    "?": ["01110", "10001", "00110", "00100", "00000", "00100"],
    "%": ["11001", "11010", "00100", "01011", "10011", "00000"],
    "°": ["0110", "1001", "0110", "0000", "0000", "0000"],
    "-": ["0000", "0000", "1111", "0000", "0000", "0000"],
    "+": ["00000", "00100", "01110", "00100", "00000", "00000"],
    "/": ["0001", "0010", "0010", "0100", "0100", "1000"],
    "&": ["01100", "10010", "01100", "10011", "10010", "01101"],
    "'": ["1", "1", "0", "0", "0", "0"],
    "(": ["010", "100", "100", "100", "100", "010"],
    ")": ["010", "001", "001", "001", "001", "010"],
    "@": ["01110", "10001", "10111", "10101", "10000", "01110"],
    "_": ["00000", "00000", "00000", "00000", "00000", "11111"],
    "#": ["01010", "11111", "01010", "01010", "11111", "01010"],
    "$": ["00100", "01111", "10100", "01110", "00101", "11110"],
    "[": ["110", "100", "100", "100", "100", "110"],
    "]": ["011", "001", "001", "001", "001", "011"],
    "=": ["0000", "1111", "0000", "1111", "0000", "0000"],
    ";": ["00", "11", "00", "00", "11", "10"],
    "<": ["001", "010", "100", "010", "001", "000"],
    ">": ["100", "010", "001", "010", "100", "000"],
    " ": ["000", "000", "000", "000", "000", "000"],
}


def sanitize_pixel_text(text: str, keep_lower_v: bool = False) -> str:
    """Normalize accents to pixel-font-safe uppercase (preserving Ñ and °)."""
    out: list[str] = []
    for ch in text:
        if keep_lower_v and ch == "v":
            out.append("v")
            continue
        if ch in ("ñ", "Ñ"):
            out.append("Ñ")
            continue
        if ch == "°":
            out.append("°")
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        up = stripped.upper()
        for c in up:
            out.append(c if c in PIXEL_FONT else " ")
    # Collapse repeated spaces
    result = "".join(out)
    while "  " in result:
        result = result.replace("  ", " ")
    return result.strip()


def pixel_text_width(text: str, scale: int, gap: int = 1) -> int:
    if not text:
        return 0
    total = 0
    for idx, ch in enumerate(text):
        glyph = PIXEL_FONT.get(ch, PIXEL_FONT.get(ch.upper(), PIXEL_FONT[" "]))
        total += len(glyph[0]) * scale
        if idx < len(text) - 1:
            total += gap * scale
    return total


def truncate_pixel_text(text: str, max_px: int, scale: int, gap: int = 1) -> str:
    clean = sanitize_pixel_text(text)
    if pixel_text_width(clean, scale, gap) <= max_px:
        return clean
    ell = ".."
    while clean and pixel_text_width(clean + ell, scale, gap) > max_px:
        clean = clean[:-1].rstrip()
    return (clean + ell) if clean else ""


def wrap_pixel_text(text: str, max_px: int, scale: int, max_lines: int = 2, gap: int = 1) -> list[str]:
    clean = sanitize_pixel_text(text)
    if not clean:
        return [""]
    words = clean.split(" ")
    lines: list[str] = []
    current = ""
    truncated = False
    for idx, word in enumerate(words):
        candidate = f"{current} {word}".strip() if current else word
        if pixel_text_width(candidate, scale, gap) <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = word
            else:
                lines.append(truncate_pixel_text(word, max_px, scale, gap))
                current = ""
            if len(lines) >= max_lines:
                truncated = bool(current) or (idx < len(words) - 1)
                current = ""
                break

    if current and len(lines) < max_lines:
        lines.append(current)

    if lines:
        if truncated and not lines[-1].endswith(".."):
            lines[-1] = truncate_pixel_text(lines[-1] + "...", max_px, scale, gap)
        else:
            lines[-1] = truncate_pixel_text(lines[-1], max_px, scale, gap)
    return lines[:max_lines]


def render_pixel_text(
    text: str,
    x: int,
    y: int,
    scale: int,
    color: str,
    align: str = "left",
    gap: int = 1,
    shadow: str | None = None,
    shadow_offset: int | None = None,
) -> str:
    w = pixel_text_width(text, scale, gap)
    if align == "right":
        start_x = x - w
    elif align == "center":
        start_x = x - w // 2
    else:
        start_x = x

    rects: list[str] = []
    if shadow:
        off = scale if shadow_offset is None else shadow_offset
        rects.append(render_pixel_text(text, x + off, y + off, scale, shadow, align=align, gap=gap, shadow=None))

    cx = start_x
    for ch in text:
        glyph = PIXEL_FONT.get(ch, PIXEL_FONT.get(ch.upper(), PIXEL_FONT[" "]))
        gw = len(glyph[0])
        for ry, row in enumerate(glyph):
            rx = 0
            while rx < gw:
                if row[rx] == "1":
                    run = 1
                    while rx + run < gw and row[rx + run] == "1":
                        run += 1
                    rects.append(
                        f'<rect x="{cx + rx * scale}" y="{y + ry * scale}" '
                        f'width="{run * scale}" height="{scale}" fill="{color}"/>'
                    )
                    rx += run
                else:
                    rx += 1
        cx += (gw + gap) * scale
    return "".join(rects)


def render_sprite(grid: list[str], palette: dict[str, str], x: int, y: int, scale: int) -> str:
    rects: list[str] = []
    for ry, row in enumerate(grid):
        rx = 0
        gw = len(row)
        while rx < gw:
            ch = row[rx]
            if ch in palette:
                run = 1
                while rx + run < gw and row[rx + run] == ch:
                    run += 1
                rects.append(
                    f'<rect x="{x + rx * scale}" y="{y + ry * scale}" '
                    f'width="{run * scale}" height="{scale}" fill="{palette[ch]}"/>'
                )
                rx += run
            else:
                rx += 1
    return "".join(rects)


def render_gbc_dialogue_box(x: int, y: int, w: int, h: int, bg: str = "#F8F8F0", cursor: bool = True) -> str:
    """Classic Pokémon Gold/Silver/Crystal double-border notched dialogue box."""
    parts = [
        f'<rect x="{x + 2}" y="{y}" width="{w - 4}" height="{h}" fill="#181820"/>',
        f'<rect x="{x}" y="{y + 2}" width="{w}" height="{h - 4}" fill="#181820"/>',
        f'<rect x="{x + 4}" y="{y + 4}" width="{w - 8}" height="{h - 8}" fill="{bg}"/>',
        f'<rect x="{x + 6}" y="{y + 6}" width="{w - 12}" height="{h - 12}" fill="#181820"/>',
        f'<rect x="{x + 8}" y="{y + 8}" width="{w - 16}" height="{h - 16}" fill="{bg}"/>',
        # Iconic 4 corner pixel ornaments
        f'<rect x="{x + 2}" y="{y + 2}" width="6" height="6" fill="#181820"/>',
        f'<rect x="{x + 4}" y="{y + 4}" width="2" height="2" fill="{bg}"/>',
        f'<rect x="{x + w - 8}" y="{y + 2}" width="6" height="6" fill="#181820"/>',
        f'<rect x="{x + w - 6}" y="{y + 4}" width="2" height="2" fill="{bg}"/>',
        f'<rect x="{x + 2}" y="{y + h - 8}" width="6" height="6" fill="#181820"/>',
        f'<rect x="{x + 4}" y="{y + h - 6}" width="2" height="2" fill="{bg}"/>',
        f'<rect x="{x + w - 8}" y="{y + h - 8}" width="6" height="6" fill="#181820"/>',
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


def render_battle_pad(
    cx: int,
    y: int,
    c_top: str = "#98C078",
    c_mid: str = "#C8E4A8",
    c_bot: str = "#78A058",
    shadow_shrink: int = 0,
) -> str:
    """Authentic Gen-2 elliptical pixel battle pad with clean contact shadow beneath the Pokémon."""
    sw = max(24, 48 - shadow_shrink * 3)
    return (
        f'<rect x="{cx - 28}" y="{y}" width="56" height="4" fill="{c_top}"/>'
        f'<rect x="{cx - 36}" y="{y + 4}" width="72" height="4" fill="{c_mid}"/>'
        f'<rect x="{cx - 40}" y="{y + 8}" width="80" height="8" fill="{c_mid}"/>'
        f'<rect x="{cx - sw // 2}" y="{y + 10}" width="{sw}" height="4" fill="{c_top}"/>'
        f'<rect x="{cx - 36}" y="{y + 16}" width="72" height="4" fill="{c_top}"/>'
        f'<rect x="{cx - 28}" y="{y + 20}" width="56" height="2" fill="{c_bot}"/>'
    )


def render_battle_bracket(x: int, y: int, w: int, h: int, border: str, accent: str) -> str:
    """Classic Pokémon Gold/Silver/Crystal tapered L-bracket HUD frame with right arrow tip."""
    by = y + h
    return (
        f'<rect x="{x}" y="{y}" width="4" height="{h + 2}" fill="{border}"/>'
        f'<rect x="{x}" y="{by}" width="{w}" height="4" fill="{border}"/>'
        f'<rect x="{x + w}" y="{by - 2}" width="4" height="6" fill="{border}"/>'
        f'<rect x="{x + w + 4}" y="{by + 2}" width="4" height="2" fill="{border}"/>'
        f'<rect x="{x + 4}" y="{by + 4}" width="{max(8, w - 6)}" height="2" fill="{accent}"/>'
    )


def render_signal_bars(x: int, y: int, color: str) -> str:
    """Authentic Gen-2 Pokégear 3-bar signal strength icon."""
    return (
        f'<rect x="{x}" y="{y + 6}" width="2" height="4" fill="{color}"/>'
        f'<rect x="{x + 4}" y="{y + 3}" width="2" height="7" fill="{color}"/>'
        f'<rect x="{x + 8}" y="{y}" width="2" height="10" fill="{color}"/>'
    )


def render_dotted_rule(x: int, y: int, w: int, color: str) -> str:
    """Crisp 8-bit dotted pixel divider."""
    parts = [f'<rect x="{px}" y="{y}" width="4" height="2" fill="{color}"/>' for px in range(x, x + w - 2, 8)]
    return "".join(parts)


def render_horizon_dither(y: int, color: str) -> str:
    """Subtle Gen-2 GBC checkerboard dither accent across the top battle stage horizon."""
    parts = [f'<rect x="{px}" y="{y}" width="4" height="2" fill="{color}"/>' for px in range(152, 236, 8)]
    parts.extend(f'<rect x="{px}" y="{y + 2}" width="4" height="2" fill="{color}"/>' for px in range(156, 236, 8))
    return "".join(parts)


# ============================================================
# Authentic 18x18 Game Boy Color Pokémon Sprites
# ============================================================

PIKACHU_SPRITE = [
    ".KK........KK.....",
    ".KKK......KKK.....",
    "..KYK....KYK......",
    "...KYKKKKYK.......",
    "...KYYYYYYK...KKK.",
    "..KYYYYYYYYK.KYYYK",
    "..KYWKYYKWYK.KYYK.",
    "..KYKKYYKKYKKYYK..",
    "..KRRYYYYRRKYYK...",
    "..KRRYYYYRRKBK....",
    "...KYYYYYYKBK.....",
    "..KYYYYYYYYK......",
    "..KYSYYYYYSK......",
    "..KYYYYYYYSK......",
    "...KYYYYYSK.......",
    "...KSYYYSSK.......",
    "..KSSKKKKSSK......",
    "..KKK....KKK......",
]
PIKACHU_PALETTE = {
    "K": "#181820", "Y": "#F8D030", "S": "#D89818",
    "W": "#FFFFFF", "R": "#F03838", "B": "#985818",
}

CHARMANDER_SPRITE = [
    "....KKKK..........",
    "...KOOOOK.........",
    "..KOOOOOOK.....R..",
    "..KOWKOOOK....RYR.",
    "..KOKKOOOK...RYYR.",
    "..KOOOOOOK...RYYR.",
    "...KOOOOOK....RKR.",
    "..KOOCCOOOK...KOK.",
    ".KOOCCCCOOK..KOOK.",
    ".KOOCCCCOOOKKOOOK.",
    "..KKCCCCOOOOOOOK..",
    "...KCCCCOOOOOOK...",
    "...KCCCCOOSOKK....",
    "...KCCCOOOOK......",
    "..KOOKKKKOOK......",
    "..KOOK..KOOK......",
    "..KWKK..KKWK......",
    "..KKK....KKK......",
]
CHARMANDER_PALETTE = {
    "K": "#181820", "O": "#F87828", "S": "#C84810",
    "C": "#F8E090", "W": "#FFFFFF", "R": "#F03020", "Y": "#F8E030",
}

SQUIRTLE_SPRITE = [
    "....KKKKK.........",
    "...KBBBBBK........",
    "..KBBBBBBBK.......",
    "..KBWKBBWKBK......",
    "..KBKKBBKKBK......",
    "..KBBBBBBBBK......",
    "...KBBBBBBK.KKKK..",
    "..KBBCCCCBKKBBBBK.",
    ".KBBCCCCCCKBBWBBK.",
    ".KBBCCCCCCKBBBBBK.",
    "..KKCCCKCCKSSBBK..",
    "...KCCCCCCKSSKK...",
    "...KCCCCCCKSSK....",
    "...KBBBBBBKKK.....",
    "..KBBK..KBBK......",
    "..KBBK..KBBK......",
    "..KWWK..KWWK......",
    "..KKKK..KKKK......",
]
SQUIRTLE_PALETTE = {
    "K": "#181820", "B": "#58B0F0", "C": "#F8E088",
    "S": "#B86830", "W": "#FFFFFF",
}

SNORLAX_SPRITE = [
    "..KK......KK......",
    ".KTTK.KK.KTTK.....",
    ".KTTKKTTKKTTK.....",
    "..KTTTTTTTTK......",
    "..KTCCCCCCKTK.....",
    ".KTCCKCCKCCKTK....",
    ".KTCCCCCCCCKTK....",
    ".KTTCCCCCCKTTK....",
    "KTTCCCCCCCCCTTK...",
    "KTTCCCCCCCCCTTK...",
    "KTTCCCCCCCCCTTK...",
    ".KKCCCCCCCCCKK....",
    "..KTCCCCCCCCK.....",
    "..KTCCCCCCCCK.....",
    ".KCCKTTTTTTKCCK...",
    ".KCWCKTTTTKCWCK...",
    ".KCCCKKKKKKCCCK...",
    "..KKK......KKK....",
]
SNORLAX_PALETTE = {
    "K": "#181820", "T": "#387888", "C": "#F8E8B8", "W": "#FFFFFF",
}

BULBASAUR_SPRITE = [
    ".......KKKK.......",
    ".....KKGGGGKK.....",
    "....KGGGGGDDGK....",
    "...KGGGGGGDDGGK...",
    "..KKGGGGGGDDGGK...",
    ".KTTKKGGGGGGGGK...",
    "KTTTTTKKKKKKGGK...",
    "KTWRTTWTTTDTTK....",
    "KTKRTTKTTTTTTK....",
    "KTTTTTTTTDDTTK....",
    ".KTTTTTTTTTTK.....",
    ".KTTDTTTTTTTK.....",
    "..KTTTTTTTTK......",
    "..KTTKKKKTTK......",
    "..KTTK..KTTK......",
    "..KWTK..KWTK......",
    "..KKKK..KKKK......",
    "..................",
]
BULBASAUR_PALETTE = {
    "K": "#181820", "G": "#48B848", "D": "#287838",
    "T": "#68C8B0", "R": "#E83838", "W": "#FFFFFF",
}

GENGAR_SPRITE = [
    ".KK..........KK...",
    ".KPK..K..K..KPK...",
    "..KPKKPKKPKKPK....",
    "..KPPPPPPPPPPK....",
    ".KPPPPPPPPPPPPK...",
    ".KPPRKPPPPRKPPK...",
    "KPPPRRKPPPRRPPPK..",
    "KPPPPPPPPPPPPPPK..",
    "KPPKWWWWWWWWKPPK..",
    "KPPKWWKWWKWWKPPK..",
    "KPPPWWWWWWWWPPPK..",
    ".KPPPPPPPPPPPPK...",
    ".KPPDDPPPPDDPPK...",
    "..KPPPPPPPPPPK....",
    "..KPPPKKKKPPPK....",
    "..KPPK....KPPK....",
    "..KKKK....KKKK....",
    "..................",
]
GENGAR_PALETTE = {
    "K": "#181820", "P": "#7858A8", "D": "#503878",
    "R": "#F03038", "W": "#FFFFFF",
}

JIGGLYPUFF_SPRITE = [
    "..KK......KK......",
    ".KPPK.KK.KPPK.....",
    ".KPPKPPPPKPPK.....",
    "..KPPKPPKPPK......",
    ".KPPPPKKPPPPK.....",
    "KPPPPPPPPPPPPK....",
    "KPPWTKPPTWKPPK.KK.",
    "KPWTTKPPTTWKPKKMMK",
    "KPPTTKPPTTKPPKKMMK",
    "KPPPPPKKPPPPPK.KK.",
    "KPPPPPPPPPPPPK.K..",
    "KPPPPPSSPPPPPKK...",
    ".KPPPPPPPPPPKK....",
    ".KPPSSPPSSPPK.....",
    "..KPPPPPPPPK......",
    "..KPPKKKKPPK......",
    "..KPK....KPK......",
    "..KKK....KKK......",
]
JIGGLYPUFF_PALETTE = {
    "K": "#181820", "P": "#F8A8C8", "S": "#E078A0",
    "T": "#30A8A8", "W": "#FFFFFF", "M": "#485868",
}

POKEBALL_SPRITE = [
    "..KKKK..",
    ".KRRRRK.",
    "KRRWRRRK",
    "KKKWWKKK",
    "KKWKKWKK",
    "KWWWWWKK",
    ".KWWWWK.",
    "..KKKK..",
]
POKEBALL_PALETTE = {"K": "#181820", "R": "#E83838", "W": "#F8F8F0"}

SPARKLE_SPRITE = [
    "...C...",
    "...C...",
    "..CWC..",
    "CCWWWCC",
    "..CWC..",
    "...C...",
    "...C...",
]
SPARKLE_PALETTE = {"C": "#3898F8", "W": "#FFFFFF"}

NOTE_SPRITE = [
    "...KKKK",
    "...KGGK",
    "...KGGK",
    "...K..K",
    ".KKK..K",
    "KGGK..K",
    "KGGK...",
    ".KK....",
]
NOTE_PALETTE = {"K": "#181820", "G": "#1DB954"}

MINI_SUN = [
    "...Y...",
    ".Y.Y.Y.",
    "..YYY..",
    "YYYYYYY",
    "..YYY..",
    ".Y.Y.Y.",
    "...Y...",
]
MINI_SUN_PAL = {"Y": "#F8B800"}

MINI_CLOUD = [
    ".......",
    "..KKK..",
    ".KWWWK.",
    "KWWWWWK",
    "KWWSSWK",
    ".KKKKK.",
    ".......",
]
MINI_CLOUD_PAL = {"K": "#586878", "W": "#FFFFFF", "S": "#C0D0E0"}

MINI_RAIN = [
    "..KKK..",
    ".KWWWK.",
    "KWWWWWK",
    ".KKKKK.",
    ".B.B.B.",
    "B.B.B..",
    ".......",
]
MINI_RAIN_PAL = {"K": "#485868", "W": "#E8F0F8", "B": "#3898F8"}

MINI_MOON = [
    "..YYY..",
    ".YY....",
    "YY.....",
    "YY.....",
    ".YY....",
    "..YYY..",
    ".......",
]
MINI_MOON_PAL = {"Y": "#F8C828"}

MINI_STORM = [
    "..KKK..",
    ".KCCCK.",
    "KCCCCCK",
    ".KKYKK.",
    "..YY...",
    "...YY..",
    "..Y....",
]
MINI_STORM_PALETTE = {"K": "#283040", "C": "#687888", "Y": "#F8D030"}
