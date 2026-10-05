import json, os, re, time
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, OpenAI

load_dotenv()


@dataclass
class Provider:
    name: str
    model: str
    client: OpenAI


def _build() -> list[Provider]:
    out = [Provider("primary", os.getenv("LLM_MODEL", "qwen3-coder"),
                    OpenAI(base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
                           api_key=os.getenv("LLM_API_KEY", "ollama"), timeout=900))]
    fb_url, fb_model = os.getenv("LLM_FALLBACK_BASE_URL"), os.getenv("LLM_FALLBACK_MODEL")
    if fb_url and fb_model:
        out.append(Provider("fallback", fb_model,
                            OpenAI(base_url=fb_url, api_key=os.getenv("LLM_FALLBACK_API_KEY") or "none", timeout=900)))
    return out


PROVIDERS = _build()
MODEL = PROVIDERS[0].model          # kept so old imports keep working
client = PROVIDERS[0].client


def record_usage(prompt_tokens: int, completion_tokens: int):
    # The API sets METER_STDOUT=1 for agent subprocesses and reads these lines to meter and cap usage
    if os.getenv("METER_STDOUT") == "1":
        print("@@USAGE " + json.dumps({"in": int(prompt_tokens), "out": int(completion_tokens)}), flush=True)


def _est(messages) -> int:
    n = 0
    for m in messages:
        n += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            n += len(tc["function"]["arguments"] or "")
    return n // 4


def _should_failover(e: Exception) -> bool:
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(e, APIStatusError) and (e.status_code >= 500 or e.status_code in (408, 429))


def _try(fn):
    for i, p in enumerate(PROVIDERS):
        try:
            return fn(p)
        except Exception as e:
            if i + 1 < len(PROVIDERS) and _should_failover(e):
                print(f"    ({p.name} model unavailable: {type(e).__name__}; trying {PROVIDERS[i + 1].name})", flush=True)
                continue
            raise


def _client(p: Provider, timeout):
    return p.client.with_options(timeout=timeout, max_retries=0) if timeout else p.client


def complete(messages, tools=None, temperature=0.2, max_tokens=6000, timeout=None):
    """One chat call (with optional tools). Returns the assistant message. Fails over and reports usage."""
    def call(p: Provider):
        kwargs = dict(model=p.model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
        return _client(p, timeout).chat.completions.create(**kwargs)

    resp = _try(call)
    u = getattr(resp, "usage", None)
    msg = resp.choices[0].message
    if u and getattr(u, "prompt_tokens", None) is not None:
        record_usage(u.prompt_tokens, u.completion_tokens or 0)
    else:
        record_usage(_est(messages), len(msg.content or "") // 4)
    return msg


def chat_ex(messages, temperature=0.3, json_mode=False, max_tokens=4096, label="", timeout=None):
    """Streaming call. Returns (text, finish_reason). Caps output length, reports usage, fails over."""
    def call(p: Provider):
        kwargs = dict(model=p.model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                      stream=True, stream_options={"include_usage": True})
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        for drop in (None, "stream_options", "response_format"):    # drop options a backend rejects
            if drop:
                kwargs.pop(drop, None)
            try:
                return _client(p, timeout).chat.completions.create(**kwargs)
            except BadRequestError:
                if drop == "response_format":
                    raise

    stream = _try(call)
    parts, n, last, finish, usage, t0 = [], 0, 0, None, None, time.time()
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue
        ch = chunk.choices[0]
        if ch.delta.content:
            parts.append(ch.delta.content)
            n += 1
            if label and n - last >= 200:
                last = n
                print(f"    ...{label} {n} tokens, {time.time() - t0:.0f}s", flush=True)
        if ch.finish_reason:
            finish = ch.finish_reason
    text = "".join(parts)
    if usage and getattr(usage, "prompt_tokens", None) is not None:
        record_usage(usage.prompt_tokens, usage.completion_tokens or 0)
    else:
        record_usage(_est(messages), len(text) // 4)
    return text, finish


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