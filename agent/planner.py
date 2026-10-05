import re
from .llm import chat, extract_json

ACCENTS = ["amber", "orange", "emerald", "teal", "sky", "indigo", "violet", "rose"]

PLANNER_PROMPT = f"""You are a senior web designer planning a single-page marketing website.
Return ONLY a JSON object with exactly this shape:
{{
  "site_name": "brand name",
  "tagline": "one line",
  "mode": "dark" or "light",
  "accent": one of {ACCENTS},
  "style": "one sentence describing the visual style",
  "sections": [
    {{"name": "PascalCaseName", "purpose": "what the section does", "content": "the concrete copy and items it must show"}}
  ]
}}
Rules:
- Between 5 and 8 sections, in page order. The first must be Navbar and the last must be Footer.
- Names: PascalCase, letters and digits only, unique. Never use App.
- "content" must contain specific copy ideas (headlines, plan names and prices, testimonials, FAQs). No lorem ipsum.
- If the user gave few details, invent plausible ones and keep everything consistent with one brand.
"""


def validate_spec(spec) -> str | None:
    if not isinstance(spec, dict):
        return "top level must be a JSON object"
    for k in ("site_name", "tagline", "mode", "accent", "style", "sections"):
        if k not in spec:
            return f"missing key: {k}"
    if spec["mode"] not in ("dark", "light"):
        return 'mode must be "dark" or "light"'
    if spec["accent"] not in ACCENTS:
        return f"accent must be one of {ACCENTS}"
    secs = spec["sections"]
    if not isinstance(secs, list) or not 4 <= len(secs) <= 9:
        return "sections must be a list with between 5 and 8 items"
    names = []
    for s in secs:
        if not isinstance(s, dict) or not all(isinstance(s.get(k), str) for k in ("name", "purpose", "content")):
            return "every section needs string fields: name, purpose, content"
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", s["name"]) or s["name"] == "App":
            return f"invalid section name: {s['name']}"
        names.append(s["name"])
    if len(set(names)) != len(names):
        return "section names must be unique"
    if names[0] != "Navbar" or names[-1] != "Footer":
        return "first section must be Navbar and last must be Footer"
    return None


def plan_site(task: str) -> dict:
    messages = [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": task}]
    for _ in range(3):
        text = chat(messages, temperature=0.4, json_mode=True, max_tokens=2000, label="plan")
        try:
            spec = extract_json(text)
            err = validate_spec(spec)
        except Exception as e:
            err = f"not valid JSON ({e})"
        if not err:
            return spec
        print(f"  plan rejected: {err}")
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"Invalid: {err}. Return the corrected JSON only."}]
    raise SystemExit("Planner could not produce a valid plan. Try rephrasing the task.")