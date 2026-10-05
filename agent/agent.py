import json
from collections import Counter
from .llm import client, MODEL
from .prompts import SYSTEM_PROMPT
from .tools import Sandbox, TOOLS, dispatch
from .verify import verify


MAX_REPAIRS = 3
KEEP_RECENT = 8   # newest messages kept untrimmed


def compact(messages):
    """Trim old, bulky tool outputs (mostly file reads) so context stays small."""
    cutoff = len(messages) - KEEP_RECENT
    for i, m in enumerate(messages):
        if i < cutoff and m["role"] == "tool" and len(m["content"]) > 400:
            m["content"] = m["content"][:400] + "\n...[older output trimmed; re-read the file if needed]"


def run_agent(project_id: str, task: str, max_steps: int = 40) -> bool:
    sb = Sandbox(project_id)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    nudges = 0
    repairs = 0
    edits = Counter()

    for step in range(1, max_steps + 1):
        compact(messages)
        resp = client.chat.completions.create(
            max_tokens=6000, model=MODEL, messages=messages, tools=TOOLS, temperature=0.2,
        )
        msg = resp.choices[0].message
        calls = msg.tool_calls or []

        assistant = {"role": "assistant", "content": msg.content or ""}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ]
        messages.append(assistant)

        if not calls:
            nudges += 1
            if nudges > 3:
                print("Model stopped calling tools. Aborting.")
                return False
            messages.append({"role": "user", "content":
                "Use a tool call. When the site is complete, call finish."})
            continue
        nudges = 0

        for c in calls:
            name = c.function.name
            raw = c.function.arguments
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except json.JSONDecodeError:
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": "ERROR: arguments were not valid JSON. Retry the call."})
                continue

            preview = args.get("path") or args.get("command") or ""
            print(f"[{step}] {name} {preview}")

            if name == "finish":
                print("    verifying build and browser...")
                problems = verify(sb)
                if not problems:
                    print(f"\nDone: {args.get('summary', '')}")
                    return True

                repairs += 1
                report = "\n\n".join(problems)[:7000]
                print(f"    verification failed ({repairs}/{MAX_REPAIRS})")
                if repairs >= MAX_REPAIRS:
                    print("\nGiving up. Last errors:\n" + report)
                    return False
                messages.append({"role": "tool", "tool_call_id": c.id, "content":
                    f"VERIFICATION FAILED (attempt {repairs}/{MAX_REPAIRS}).\n\n{report}\n\n"
                    "HOW TO FIX: (1) If there is a stack trace, open the file it names. "
                    "(2) Otherwise call search_files with the exact error message text to find where it comes from. "
                    "(3) Fix that root cause. Do NOT rewrite files unrelated to the error. "
                    "Then call finish again."})
                continue

            result = dispatch(sb, name, args)
            if name in ("write_file", "replace_in_file") and not result.startswith("ERROR"):
                p = args.get("path", "")
                edits[p] += 1
                if edits[p] > 3:
                    result += (f"\n\nWARNING: you have edited {p} {edits[p]} times in this run. "
                               "Stop rewriting it. Re-read the exact error, look for the root cause in OTHER "
                               "files (use search_files), or call finish if the work is done.")
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})

    print(f"Reached {max_steps} steps without finishing.")
    return False