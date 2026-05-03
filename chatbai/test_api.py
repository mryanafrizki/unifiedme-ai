"""Quick test of ChatBAI API — chat + usage check."""
import json
import sys
import httpx
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_KEY = "sk-3o824z099vb0188e0dxut2xfahc028qa"
PROXY = "http://192.168.18.25:9016"
BASE = "https://api.b.ai"


async def main():
    client = httpx.AsyncClient(proxy=PROXY, timeout=30)
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

    # Test 1: Chat with glm-5 (free model)
    print("=== Test 1: glm-5 (free) ===")
    resp = await client.post(
        f"{BASE}/v1/chat/completions",
        headers=headers,
        json={"model": "glm-5", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    data = resp.json()
    usage = data.get("usage", {})
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    print(f"  Model: {data.get('model')}")
    print(f"  Tokens: prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}, total={usage.get('total_tokens')}")
    print(f"  Response: {content[:80]}")
    print()

    # Test 2: Chat with gpt-5-mini (free model)
    print("=== Test 2: gpt-5-mini (free) ===")
    resp = await client.post(
        f"{BASE}/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-5-mini", "messages": [{"role": "user", "content": "say hello"}], "stream": False},
    )
    data = resp.json()
    usage = data.get("usage", {})
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    print(f"  Model: {data.get('model')}")
    print(f"  Tokens: prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}, total={usage.get('total_tokens')}")
    print(f"  Response: {content[:80]}")
    print()

    # Test 3: Try premium model (should fail or deduct more)
    print("=== Test 3: claude-opus-4.6 (premium) ===")
    resp = await client.post(
        f"{BASE}/v1/chat/completions",
        headers=headers,
        json={"model": "claude-opus-4.6", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    data = resp.json()
    if "error" in data:
        print(f"  Error: {json.dumps(data['error'], indent=2)}")
    else:
        usage = data.get("usage", {})
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  Model: {data.get('model')}")
        print(f"  Tokens: prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}")
        print(f"  Response: {content[:80]}")
    print()

    # Test 4: Check if there's a usage/balance endpoint
    print("=== Test 4: Check balance endpoints ===")
    for path in ["/v1/dashboard/billing/usage", "/v1/usage", "/v1/balance", "/v1/credits"]:
        resp = await client.get(f"{BASE}{path}", headers={"x-api-key": API_KEY})
        print(f"  {path}: {resp.status_code} {resp.text[:100]}")

    await client.aclose()


asyncio.run(main())
