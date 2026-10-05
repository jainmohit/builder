import html, json, re, time
from .llm import chat_ex, extract_code
from .planner import plan_site
from .tools import PROJECTS, Sandbox

ALLOWED_PACKAGES = {"react"}


def component_system_prompt(spec: dict) -> str:
    accent, mode = spec["accent"], spec["mode"]
    hint = ("dark backgrounds (neutral-950 / neutral-900) with light text" if mode == "dark"
            else "white / neutral-50 backgrounds with dark text")
    return f"""You write ONE React + TypeScript component file for a website that uses Tailwind CSS v4.
OUTPUT: only the complete file contents inside a single ```tsx code block. No explanation.

HARD RULES:
- The file must contain a default-exported function component with a body, and it takes no props.
- Import only from "react", and only if you need hooks. NO other packages, no icon libraries, no images,
  no remote fonts. Use inline SVG or emoji for icons.
- Tailwind utility classes only. Mobile first and responsive (sm:, md:, lg:).
- Accent color: use {accent}-500 and {accent}-600 classes. Page mode is {mode}: use {hint}.
- Render only your own section, wrapped in <section> (use <header> for Navbar, <footer> for Footer).
- Typical container: max-w-6xl mx-auto px-6 py-20.
- Strict TypeScript: type your state and arrays, and leave no unused variables or imports.
- Write real, specific copy. No lorem ipsum.
- NEVER use <img> tags or url(...). For photo-like areas use a gradient div
  (e.g. bg-gradient-to-br from-{accent}-100 to-{accent}-300) with an emoji or inline SVG on top.
- Never import React itself. Use named imports only: import {{ useState }} from "react".
"""


def check_component(code: str, name: str) -> str | None:
    if len(code) < 200:
        return "the file is too short"
    if "export default" not in code:
        return f"it must contain `export default function {name}()`"
    for pkg in re.findall(r"^import[^'\"]*from\s+['\"]([^'\"]+)['\"]", code, re.M):
        if not pkg.startswith(".") and pkg not in ALLOWED_PACKAGES:
            return f"it imports the disallowed package '{pkg}'. Only react is allowed"
    if re.search(r"<img\b|url\(", code):
        return "do not use <img> tags or url(). There are no image files. Use a gradient div, emoji or inline SVG"
    return None


def stub(name: str) -> str:
    return (f'export default function {name}() {{\n'
            f'  return <section className="max-w-6xl mx-auto px-6 py-20"><h2 className="text-3xl font-bold">{name}</h2></section>\n'
            f'}}\n')


def generate_component(spec: dict, section: dict) -> str:
    name = section["name"]
    others = ", ".join(s["name"] for s in spec["sections"] if s["name"] != name)
    user = (f"Site: {spec['site_name']} - {spec['tagline']}\n"
            f"Visual style: {spec['style']}\n\n"
            f"Write the component named {name}.\n"
            f"Purpose: {section['purpose']}\n"
            f"It must show: {section['content']}\n\n"
            f"Other sections exist on the page (do not build them): {others}")
    messages = [{"role": "system", "content": component_system_prompt(spec)},
                {"role": "user", "content": user}]
    for _ in range(3):
        text, finish = chat_ex(messages, temperature=0.3, max_tokens=4096, label=name)
        code = sanitize(extract_code(text))
        if finish == "length":
            err = "the output was cut off for being too long. Write a shorter, simpler component (under about 150 lines)"
        else:
            err = check_component(code, name)
        if not err:
            return code
        print(f"    {name} rejected: {err}")
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"Problem: {err}. Output the corrected complete file in one ```tsx block."}]
    print(f"    {name}: using a stub, the repair agent will deal with it")
    return stub(name)


def assemble_app(spec: dict) -> str:
    names = [s["name"] for s in spec["sections"]]
    middle = [n for n in names if n not in ("Navbar", "Footer")]
    base = "bg-neutral-950 text-neutral-100" if spec["mode"] == "dark" else "bg-white text-neutral-900"
    lines = [f"import {n} from './components/{n}'" for n in names]
    lines += ["", "export default function App() {", "  return (",
              f'    <div className="min-h-screen {base}">', "      <Navbar />", "      <main>"]
    lines += [f"        <{n} />" for n in middle]
    lines += ["      </main>", "      <Footer />", "    </div>", "  )", "}", ""]
    return "\n".join(lines)


def is_fresh(project_id: str) -> bool:
    app = PROJECTS / project_id / "src" / "App.tsx"
    return app.is_file() and "Base template works" in app.read_text()


def build_site(project_id: str, task: str) -> dict:
    sb = Sandbox(project_id)

    print("Planning...")
    spec = plan_site(task)
    print(f"  {spec['site_name']} | {spec['mode']} / {spec['accent']}")
    print("  sections: " + ", ".join(s["name"] for s in spec["sections"]))
    sb.write_file(".builder/spec.json", json.dumps(spec, indent=2))

    for i, s in enumerate(spec["sections"], 1):
        print(f"Writing {s['name']} ({i}/{len(spec['sections'])})...", flush=True)
        t0 = time.time()
        sb.write_file(f"src/components/{s['name']}.tsx", generate_component(spec, s))
        print(f"  done in {time.time() - t0:.0f}s", flush=True)

    sb.write_file("src/App.tsx", assemble_app(spec))

    index = sb.root / "index.html"
    if index.is_file():
        title = html.escape(spec["site_name"])
        index.write_text(re.sub(r"<title>.*?</title>", f"<title>{title}</title>", index.read_text(), flags=re.S))
    return spec

def sanitize(code: str) -> str:
    # The Vite template uses the new JSX transform: an unused `React` import fails the build
    if not re.search(r"\bReact\.", code):
        code = re.sub(r"^import React,\s*\{([^}]*)\}\s*from\s*['\"]react['\"];?[ \t]*$",
                      r"import {\1} from 'react'", code, flags=re.M)
        code = re.sub(r"^import React\s+from\s*['\"]react['\"];?[ \t]*\n", "", code, flags=re.M)
    return code