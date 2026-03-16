"""
Compresses text using The Token Company API (bear-1.1) before sending to LLMs.
Falls back to raw text if API key is not set or the call fails.
"""
import os
import urllib.request
import urllib.error
import json

_API_KEY = None


def _get_api_key():
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = os.getenv("TOKEN_COMPANY_API_KEY")
    return _API_KEY


def compress(text: str, aggressiveness: float = 0.5) -> str:
    """
    Returns compressed version of text. Falls back to original text on any failure.
    aggressiveness: 0.0 (light) to 1.0 (heavy), default 0.5
    """
    api_key = _get_api_key()
    if not api_key or not text:
        return text

    payload = json.dumps({
        "model": "bear-1.1",
        "input": text,
        "compression_settings": {"aggressiveness": aggressiveness}
    }).encode()

    req = urllib.request.Request(
        "https://api.thetokencompany.com/v1/compress",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        compressed = result.get("output", text)
        orig = result.get("original_input_tokens", "?")
        out = result.get("output_tokens", "?")
        print(f"Compressed {orig} → {out} tokens")
        return compressed
    except Exception as e:
        print(f"Compressor error: {e}")
        return text
