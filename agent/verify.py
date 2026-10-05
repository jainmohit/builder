import sys
import re
from pathlib import Path
from .tools import Sandbox

SHOTS = Path(__file__).resolve().parent.parent / "screenshots"
IGNORE_CONSOLE = ("favicon",)   # add substrings here for known-harmless console errors


def check_build(sb: Sandbox) -> list[str]:
    # The Vite template's build script is "tsc -b && vite build",
    # so this covers type errors and bundling errors together.
    code, out = sb.exec("npm run build 2>&1", timeout=180, limit=6000)
    if code == 0:
        return []
    return [f"BUILD FAILED (npm run build, exit {code}):\n{out}"]


def check_browser(sb: Sandbox) -> list[str]:
    from playwright.sync_api import sync_playwright
    try:
        port = sb.port()
    except RuntimeError as e:
        return [str(e)]

    problems: list[str] = []

    def on_pageerror(err):
        stack = getattr(err, "stack", None) or str(err)
        stack = re.sub(r"https?://localhost:\d+/", "", stack)   # keep just src/... paths
        problems.append(f"Uncaught runtime error: {err}\nStack trace:\n{stack[:1200]}")

    def on_console(msg):
        text = msg.text
        if msg.type == "error" and not any(s in text.lower() for s in IGNORE_CONSOLE):
            problems.append(f"console.error: {text[:300]}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", on_pageerror)
        page.on("console", on_console)
        try:
            page.goto(f"http://localhost:{port}", wait_until="load", timeout=30000)
            page.wait_for_timeout(1500)   # let hot reload and React settle
        except Exception as e:
            browser.close()
            return problems + [f"Page failed to load: {e}"]

        overlay = page.evaluate("""() => {
            const o = document.querySelector('vite-error-overlay');
            return o && o.shadowRoot ? o.shadowRoot.textContent.slice(0, 1500) : null;
        }""")
        if overlay:
            problems.append(f"Vite error overlay is showing:\n{overlay}")

        if len(page.inner_text("body").strip()) < 5:
            problems.append("The page renders blank (no visible text).")
        
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(600)    # trigger lazy-loaded images
        broken = page.evaluate("""() => [...document.images]
            .filter(i => !i.complete || i.naturalWidth === 0)
            .map(i => i.getAttribute('src') || '(no src)').slice(0, 5)""")
        if broken:
            problems.append("Broken images (files missing or failed to load): " + ", ".join(broken)
                            + ". Replace the <img> tags with a gradient div, emoji or inline SVG.")

        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{sb.id}.png"), full_page=True)
        browser.close()

    return list(dict.fromkeys(problems))[:8]   # de-duplicate, cap the list


def verify(sb: Sandbox, use_browser: bool = True) -> list[str]:
    problems = check_build(sb)
    if not problems and use_browser:   # no point loading the page if the build is broken
        problems = check_browser(sb)
    return problems


if __name__ == "__main__":
    problems = verify(Sandbox(sys.argv[1]))
    print("PASS" if not problems else "FAIL\n\n" + "\n\n".join(problems))