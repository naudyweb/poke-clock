#!/usr/bin/env python3
"""
poke-ctl: Instant CLI controller for Poké-Hub (http://127.0.0.1:8765).

Examples:
  poke-ctl status
  poke-ctl show gemini
  poke-ctl show claude
  poke-ctl show clock
  poke-ctl auto
  poke-ctl notify "BUILD OK" "Tests 100% verdes" --pokemon pikachu --ttl 12
  poke-ctl brightness 80
  poke-ctl display on
  poke-ctl rotation 2
  poke-ctl toggle weather off
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HUB_URL = os.environ.get("POKE_HUB_URL", "http://127.0.0.1:8765")


def call_api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(f"{HUB_URL}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8"))
            print(f"Error ({exc.code}): {err.get('error', err)}", file=sys.stderr)
        except Exception:
            print(f"HTTP Error {exc.code}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Cannot reach Poké-Hub at {HUB_URL}: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(prog="poke-ctl", description="Control Poké-Hub desktop display")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show live status")
    sub.add_parser("auto", help="Resume automatic rotation")
    sub.add_parser("refresh", help="Refresh Gemini, Claude, Weather, and Spotify data")

    p_show = sub.add_parser("show", help="Immediately show and pin a screen")
    p_show.add_argument("card", choices=["clock", "weather", "gemini", "claude", "spotify"])

    p_not = sub.add_parser("notify", help="Display a temporary Pokémon alert")
    p_not.add_argument("title", help="Alert title")
    p_not.add_argument("message", nargs="?", default="", help="Alert message body")
    p_not.add_argument("--pokemon", default="pikachu", choices=["pikachu", "charmander", "squirtle", "bulbasaur", "gengar", "snorlax", "jigglypuff"])
    p_not.add_argument("--ttl", type=int, default=12, help="Seconds before returning to normal view")

    p_br = sub.add_parser("brightness", help="Set display brightness (0..100)")
    p_br.add_argument("level", type=int)

    p_display = sub.add_parser("display", help="Turn the physical display on or off")
    p_display.add_argument("state", choices=["on", "off"])

    p_rot = sub.add_parser("rotation", help="Set display rotation (0..3)")
    p_rot.add_argument("val", type=int, choices=[0, 1, 2, 3])

    p_tog = sub.add_parser("toggle", help="Enable/disable a card, spotify_priority, or animations")
    p_tog.add_argument("target", choices=["clock", "weather", "gemini", "claude", "spotify_priority", "animations"])
    p_tog.add_argument("state", choices=["on", "off"])

    p_pal = sub.add_parser("palette", help="Switch Pokémon retro color palette")
    p_pal.add_argument("name", choices=["gbc_classic", "gbc_dark", "gb_green", "pokedex_red"])

    p_sp = sub.add_parser("sprite", help="Change Pokémon sprite for a screen")
    p_sp.add_argument("screen", choices=["clock_day", "clock_night", "weather", "gemini", "claude", "spotify"])
    p_sp.add_argument("pokemon", choices=["auto", "pikachu", "charmander", "squirtle", "bulbasaur", "gengar", "snorlax", "jigglypuff"])

    args = parser.parse_args()

    if args.cmd == "status":
        st = call_api("GET", "/api/status")
        print(json.dumps(st, indent=2, ensure_ascii=False))
    elif args.cmd == "auto":
        print(json.dumps(call_api("POST", "/api/auto"), ensure_ascii=False))
    elif args.cmd == "refresh":
        print(json.dumps(call_api("POST", "/api/refresh"), ensure_ascii=False))
    elif args.cmd == "show":
        print(json.dumps(call_api("POST", "/api/show", {"card": args.card}), ensure_ascii=False))
    elif args.cmd == "notify":
        print(json.dumps(call_api("POST", "/api/notify", {
            "title": args.title,
            "message": args.message,
            "pokemon": args.pokemon,
            "ttl": args.ttl,
        }), ensure_ascii=False))
    elif args.cmd == "brightness":
        print(json.dumps(call_api("POST", "/api/brightness", {"val": args.level}), ensure_ascii=False))
    elif args.cmd == "display":
        print(json.dumps(call_api("POST", "/api/display", {"on": args.state == "on"}), ensure_ascii=False))
    elif args.cmd == "rotation":
        print(json.dumps(call_api("POST", "/api/rotation", {"val": args.val}), ensure_ascii=False))
    elif args.cmd == "toggle":
        val = args.state == "on"
        if args.target in ("spotify_priority", "animations"):
            patch = {args.target: val}
        else:
            patch = {"enabled_cards": {args.target: val}}
        print(json.dumps(call_api("POST", "/api/config", patch), ensure_ascii=False))
    elif args.cmd == "palette":
        print(json.dumps(call_api("POST", "/api/config", {"palette": args.name}), ensure_ascii=False))
    elif args.cmd == "sprite":
        print(json.dumps(call_api("POST", "/api/config", {"sprites": {args.screen: args.pokemon}}), ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
