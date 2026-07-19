#!/usr/bin/env python3
"""Generate neofetch-style SVG profile cards from GitHub data."""

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import yaml
from PIL import Image


def load_config():
    with open(Path(__file__).parent / "config.yaml") as f:
        return yaml.safe_load(f)


def fetch_github_profile(username):
    result = subprocess.run(
        ["gh", "api", f"users/{username}"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def fetch_pinned_repos(username):
    query = (
        '{ user(login: "%s") { pinnedItems(first: 6, types: REPOSITORY) '
        '{ nodes { ... on Repository { name description stargazerCount '
        'primaryLanguage { name } } } } } }' % username
    )
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return data["data"]["user"]["pinnedItems"]["nodes"]


def fetch_top_languages(username, limit=5):
    query = (
        '{ user(login: "%s") { repositories(first: 100, isFork: false, '
        'ownerAffiliations: OWNER, orderBy: {field: UPDATED_AT, direction: DESC}) '
        '{ nodes { languages(first: 10) { edges { size node { name } } } } } } }'
        % username
    )
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    repos = data["data"]["user"]["repositories"]["nodes"]

    bytes_per_lang = {}
    for repo in repos:
        for edge in repo.get("languages", {}).get("edges", []):
            name = edge["node"]["name"]
            bytes_per_lang[name] = bytes_per_lang.get(name, 0) + edge["size"]

    sorted_langs = sorted(bytes_per_lang.items(), key=lambda x: x[1], reverse=True)
    top = sorted_langs[:limit]
    total = sum(b for _, b in top)
    return [(name, round(b / total * 100)) for name, b in top] if total else []


def fetch_stackoverflow_rep(user_id):
    url = f"https://api.stackexchange.com/2.3/users/{user_id}?site=stackoverflow"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    if data.get("items"):
        return data["items"][0].get("reputation", 0)
    return 0


def compute_account_age(profile):
    created = profile.get("created_at", "")
    if not created:
        return "N/A"
    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    years = now.year - created_dt.year
    if (now.month, now.day) < (created_dt.month, created_dt.day):
        years -= 1
    if years < 1:
        return "< 1 year"
    return f"{years}+ years"


def fetch_avatar(username, size=200):
    url = f"https://github.com/{username}.png?size={size}"
    with urllib.request.urlopen(url) as resp:
        return Image.open(BytesIO(resp.read()))


def remove_background(img):
    """Replace the dominant background color with white."""
    img = img.convert("RGBA")
    pixels = list(img.getdata())
    w, h = img.size

    # sample corners to find the background color
    corner_pixels = []
    for cy in range(min(5, h)):
        for cx in range(min(5, w)):
            corner_pixels.append(img.getpixel((cx, cy)))
            corner_pixels.append(img.getpixel((w - 1 - cx, cy)))
            corner_pixels.append(img.getpixel((cx, h - 1 - cy)))
            corner_pixels.append(img.getpixel((w - 1 - cx, h - 1 - cy)))

    avg_r = sum(p[0] for p in corner_pixels) // len(corner_pixels)
    avg_g = sum(p[1] for p in corner_pixels) // len(corner_pixels)
    avg_b = sum(p[2] for p in corner_pixels) // len(corner_pixels)

    threshold = 80
    new_pixels = []
    for p in pixels:
        dist = ((p[0] - avg_r) ** 2 + (p[1] - avg_g) ** 2 + (p[2] - avg_b) ** 2) ** 0.5
        if dist < threshold:
            new_pixels.append((255, 255, 255, 255))
        else:
            new_pixels.append(p)

    img.putdata(new_pixels)
    return img.convert("RGB")


def image_to_ascii(img, width=35, chars="@%#*+=-:. "):
    img = remove_background(img)

    aspect_ratio = img.height / img.width
    height = int(width * aspect_ratio * 0.55)
    img = img.resize((width, height)).convert("L")

    from PIL import ImageEnhance
    img = ImageEnhance.Contrast(img).enhance(1.8)

    lines = []
    for y in range(height):
        line = ""
        for x in range(width):
            pixel = img.getpixel((x, y))
            idx = pixel * (len(chars) - 1) // 255
            line += chars[idx]
        lines.append(line)
    return lines


def escape_xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")


def resolve_fields(section, profile, pinned, top_langs=None, extras=None):
    """Resolve dynamic field values from GitHub data."""
    if section.get("source") == "top_languages":
        langs = top_langs or []
        if not langs:
            return []
        bar_width = 15
        fields = []
        for name, pct in langs:
            filled = round(pct / 100 * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            fields.append({"key": name, "value": f"{bar} {pct}%"})
        return fields

    if section.get("source") == "pinned_repos":
        fields = []
        for repo in pinned:
            lang = repo.get("primaryLanguage")
            lang_name = lang["name"] if lang else "N/A"
            desc = repo.get("description") or lang_name
            if len(desc) > 22:
                desc = desc[:20] + ".."
            fields.append({"key": repo["name"], "value": desc})
        return fields

    resolved = []
    for field in section.get("fields", []):
        key = field["key"]
        value = field["value"]

        if value == "from_bio":
            value = profile.get("bio") or "N/A"
        elif value == "from_github":
            ext = extras or {}
            mapping = {
                "Repos": str(profile.get("public_repos", 0)),
                "Followers": str(profile.get("followers", 0)),
                "Following": str(profile.get("following", 0)),
                "Blog": profile.get("blog") or "N/A",
                "Location": profile.get("location") or "N/A",
                "Company": profile.get("company") or "N/A",
                "Twitter": profile.get("twitter_username") or "N/A",
                "Coding Since": ext.get("account_age", "N/A"),
                "SO Rep": ext.get("so_rep", "N/A"),
            }
            value = mapping.get(key, "N/A")

        if len(value) > 25:
            value = value[:23] + ".."

        resolved.append({"key": key, "value": value})
    return resolved


def build_right_panel(config, profile, pinned, top_langs=None, extras=None):
    """Build the list of tspan elements for the right-side info panel."""
    username = config["username"]
    name = profile.get("name") or username
    lines = []
    y = 30

    header_line = f"{name}@{username}"
    separator = "─" * (50 - len(header_line))
    lines.append(("header", y, f"{header_line} {separator}"))
    y += 25

    for section in config["sections"]:
        title = section.get("title")
        fields = resolve_fields(section, profile, pinned, top_langs, extras)

        if not fields and title:
            continue

        if title:
            y += 5
            sep = "─" * (48 - len(title))
            lines.append(("section", y, f"─ {title} {sep}"))
            y += 22

        for field in fields:
            key = field["key"]
            value = field["value"]
            dot_count = max(2, 28 - len(key))
            dots = "." * dot_count
            lines.append(("field", y, key, dots, value))
            y += 20

        y += 5

    return lines, y


def generate_svg(config, profile, pinned, ascii_lines, theme, top_langs=None, extras=None):
    colors = config["style"][theme]
    right_panel, panel_height = build_right_panel(config, profile, pinned, top_langs, extras)

    ascii_height = len(ascii_lines) * 22 + 30
    svg_height = max(ascii_height, panel_height + 30)
    svg_width = 985

    svg = f"""<?xml version='1.0' encoding='UTF-8'?>
<svg xmlns="http://www.w3.org/2000/svg" font-family="ConsolasFallback,Consolas,monospace" width="{svg_width}px" height="{svg_height}px" font-size="16px">
<style>
@font-face {{
src: local('Consolas'), local('Consolas Bold');
font-family: 'ConsolasFallback';
font-display: swap;
-webkit-size-adjust: 109%;
size-adjust: 109%;
}}
.key {{fill: {colors['key']};}}
.value {{fill: {colors['value']};}}
.cc {{fill: {colors['muted']};}}
text, tspan {{white-space: pre;}}
</style>
<rect width="{svg_width}px" height="{svg_height}px" fill="{colors['bg']}" rx="15"/>
<text x="15" y="30" fill="{colors['text']}" font-size="18px">
"""

    for i, line in enumerate(ascii_lines):
        y = 25 + i * 22
        svg += f'<tspan x="15" y="{y}">{escape_xml(line)}</tspan>\n'

    svg += '</text>\n<text x="390" y="30" fill="' + colors["text"] + '">\n'

    for item in right_panel:
        if item[0] == "header":
            _, y, text = item
            svg += f'<tspan x="390" y="{y}">{escape_xml(text)}</tspan>\n'
        elif item[0] == "section":
            _, y, text = item
            svg += f'<tspan x="390" y="{y}" class="key">{escape_xml(text)}</tspan>\n'
        elif item[0] == "field":
            _, y, key, dots, value = item
            svg += (
                f'<tspan x="390" y="{y}" class="cc">. </tspan>'
                f'<tspan class="key">{escape_xml(key)}</tspan>:'
                f'<tspan class="cc"> {dots} </tspan>'
                f'<tspan class="value">{escape_xml(value)}</tspan>\n'
            )

    svg += "</text>\n</svg>\n"
    return svg


def main():
    config = load_config()
    username = config["username"]
    ascii_cfg = config.get("ascii", {})

    print(f"Fetching GitHub profile for {username}...")
    profile = fetch_github_profile(username)

    print("Fetching pinned repos...")
    pinned = fetch_pinned_repos(username)

    print("Fetching top languages...")
    top_langs = fetch_top_languages(username)

    extras = {}
    extras["account_age"] = compute_account_age(profile)

    so_user_id = config.get("stackoverflow_id")
    if so_user_id:
        print("Fetching Stack Overflow reputation...")
        so_rep = fetch_stackoverflow_rep(so_user_id)
        extras["so_rep"] = f"{so_rep:,}"

    print("Fetching avatar...")
    avatar = fetch_avatar(username)

    print("Converting to ASCII art...")
    ascii_lines = image_to_ascii(
        avatar,
        width=ascii_cfg.get("width", 35),
        chars=ascii_cfg.get("chars", "@%#*+=-:. "),
    )

    base = Path(__file__).parent
    for theme in ("dark", "light"):
        print(f"Generating {theme}_mode.svg...")
        svg = generate_svg(config, profile, pinned, ascii_lines, theme, top_langs, extras)
        (base / f"{theme}_mode.svg").write_text(svg)

    print("Done!")


if __name__ == "__main__":
    main()
