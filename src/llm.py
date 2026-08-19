"""OpenAI-compatible chat completion via stdlib (no extra dependencies)."""
import json
import time
import urllib.error
import urllib.request


def chat(messages: list[dict], *, model: str, api_key: str, base_url: str,
         max_tokens: int = 49_152, retries: int = 3) -> str:
    """POST {base_url}/chat/completions. Retry up to `retries` times (timeout/429/5xx).

    Default max_tokens 49152, matching the verify agent: deepseek-v4-flash is a
    reasoning model that spends most of its budget on reasoning_content before
    emitting any real content, so the cap is mostly consumed before the answer
    starts. 4096 returned empty content; 16384 got a 120-file PR roughly 7.6K
    characters into its claims JSON and cut it mid-string.
    """
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }).encode()

    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
                choice = data["choices"][0]
                content = choice["message"]["content"]
                if content is None:
                    raise RuntimeError("chat returned null content")
                # Truncation has to be named here. The caller only sees a
                # half-written document, and json.loads reports it as
                # "Unterminated string at line 146" — which reads like the model
                # returned garbage rather than like it ran out of room.
                if choice.get("finish_reason") == "length":
                    raise RuntimeError(
                        f"chat truncated: hit max_tokens={max_tokens} before "
                        f"finishing (finish_reason=length, {len(content)} chars "
                        f"returned). Raise max_tokens or send less input.")
                return content
        except urllib.error.HTTPError as e:
            retryable = e.code >= 500 or e.code == 429
            if not retryable:
                raise RuntimeError(
                    f"chat failed (HTTP {e.code}): "
                    f"{e.read().decode(errors='replace')[:300]}"
                ) from e
            last_err = e
            retry_after = e.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
                continue
        except urllib.error.URLError as e:
            last_err = e
        except KeyError as e:
            raise RuntimeError("chat returned malformed response: missing key "
                               f"{e}") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"chat returned non-JSON response: {e}") from e
        time.sleep(2 * attempt)
    raise RuntimeError(f"chat failed after {retries} retries: {last_err}")
