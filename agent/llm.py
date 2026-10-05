import json, os, re, sys, time
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
    api_key=os.getenv("LLM_API_KEY", "ollama"),
    timeout=900,
)
MODEL = os.getenv("LLM_MODEL", "qwen3-coder")


def chat_ex(messages, temperature=0.3, json_mode=False, max_tokens=4096, label=""):
    """Returns (text, finish_reason). Streams so progress is visible, and caps output length."""
    kwargs = dict(model=MODEL, messages=messages, temperature=temperature,
                  max_tokens=max_tokens, stream=True)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        stream = client.chat.completions.create(**kwargs)
    except Exception:
        if not json_mode:
            raise
        kwargs.pop("response_format")        # some backends don't support JSON mode
        stream = client.chat.completions.create(**kwargs)

    parts, n, last, finish, t0 = [], 0, 0, None, time.time()
    for chunk in stream:
        if not chunk.choices:
            continue
        ch = chunk.choices[0]
        if ch.delta.content:
            parts.append(ch.delta.content)
            n += 1
            if n - last >= 200:
                last = n
                print(f"    ...{label} {n} tokens, {time.time() - t0:.0f}s", flush=True)
        if ch.finish_reason:
            finish = ch.finish_reason
    return "".join(parts), finish


def chat(*args, **kwargs) -> str:
    return chat_ex(*args, **kwargs)[0]


def extract_json(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(text[s:e + 1])


def extract_code(text: str) -> str:
    m = re.search(r"```(?:tsx|ts|jsx|js|typescript|javascript)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"