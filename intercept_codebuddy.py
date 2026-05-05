#!/usr/bin/env python3
"""
Intercept CodeBuddy API to discover available models (especially gpt-5.5).

Usage:
    python intercept_codebuddy.py --api-key YOUR_CB_API_KEY

    Or with email/password (will do browser login to get API key):
    python intercept_codebuddy.py --email you@gmail.com --password yourpass

What it does:
  1. Tries to list available models from CodeBuddy's API
  2. Sends test requests with various gpt-5.5 model name variants
  3. Captures responses to discover the correct model name

Press Ctrl+C to stop.
"""

import argparse
import asyncio
import json
import os
import sys
import time

import httpx

CODEBUDDY_BASE_URL = os.getenv("CODEBUDDY_BASE_URL", "https://www.codebuddy.ai")
CHAT_ENDPOINT = f"{CODEBUDDY_BASE_URL}/v2/chat/completions"

# Headers matching what proxy_codebuddy.py uses
def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "CLI/2.54.0 CodeBuddy/2.54.0",
        "X-Domain": "www.codebuddy.ai",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-Agent-Intent": "craft",
        "Accept": "text/event-stream",
    }


# Model name variants to try for gpt-5.5
GPT55_VARIANTS = [
    # Direct names
    "gpt-5.5",
    "gpt-5.5-preview",
    "gpt-5.5-turbo",
    "gpt-5.5-mini",
    "gpt-5.5-nano",
    "gpt-5.5-codex",
    # OpenAI naming patterns
    "o3-pro",
    "o3",
    "o4-mini",
    "o4",
    # Alternative CB naming (they use dots)
    "gpt5.5",
    "openai-gpt-5.5",
    "chatgpt-5.5",
    # Maybe they use a different version scheme
    "gpt-5-5",
    "gpt55",
    # Check if it's under a different brand
    "gpt-5.4-pro",  # maybe 5.5 is actually 5.4-pro?
    "gpt-5.4-max",
]

# Known working models (for comparison/validation)
KNOWN_MODELS = [
    "gpt-5.4",
    "gpt-5.2",
    "gpt-5.1",
    "claude-opus-4.6",
    "gemini-2.5-pro",
    "deepseek-v3.2",
]


async def test_model(client: httpx.AsyncClient, headers: dict, model: str) -> dict:
    """Send a minimal chat request with a given model name and capture the response."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "stream": True,
        "max_tokens": 100,
        "temperature": 0.7,
    }

    print(f"\n{'='*60}")
    print(f"  Testing model: {model}")
    print(f"{'='*60}")

    result = {
        "model": model,
        "status": None,
        "error": None,
        "response_model": None,
        "content": "",
        "usage": None,
        "raw_chunks": [],
    }

    try:
        req = client.build_request(
            method="POST",
            url=CHAT_ENDPOINT,
            json=body,
            headers=headers,
        )
        resp = await client.send(req, stream=True)
        result["status"] = resp.status_code

        if resp.status_code >= 400:
            error_body = await resp.aread()
            await resp.aclose()
            try:
                error_json = json.loads(error_body)
                result["error"] = error_json
                print(f"  [FAIL] HTTP {resp.status_code}: {json.dumps(error_json, indent=2)[:500]}")
            except (json.JSONDecodeError, ValueError):
                result["error"] = error_body.decode(errors="replace")[:500]
                print(f"  [FAIL] HTTP {resp.status_code}: {result['error']}")
            return result

        # Parse SSE stream
        content_parts = []
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                result["raw_chunks"].append(chunk)

                # Capture model name from response
                if "model" in chunk and not result["response_model"]:
                    result["response_model"] = chunk["model"]

                # Capture content
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        content_parts.append(content)

                # Capture usage
                usage = chunk.get("usage") or {}
                if usage.get("total_tokens", 0) > 0:
                    result["usage"] = usage
            except (json.JSONDecodeError, ValueError):
                pass

        await resp.aclose()
        result["content"] = "".join(content_parts)

        print(f"  [OK] HTTP {resp.status_code}")
        print(f"  Response model: {result['response_model']}")
        print(f"  Content: {result['content'][:200]}")
        if result["usage"]:
            print(f"  Usage: {json.dumps(result['usage'])}")

    except httpx.ConnectError as e:
        result["status"] = 0
        result["error"] = f"Connection error: {e}"
        print(f"  [FAIL] Connection error: {e}")
    except httpx.TimeoutException as e:
        result["status"] = 0
        result["error"] = f"Timeout: {e}"
        print(f"  [FAIL] Timeout: {e}")
    except Exception as e:
        result["status"] = 0
        result["error"] = f"Error: {e}"
        print(f"  [FAIL] Error: {e}")

    return result


async def try_list_models(client: httpx.AsyncClient, headers: dict) -> list[str]:
    """Try various endpoints to discover available models."""
    models_found = []

    # Try common model listing endpoints
    endpoints = [
        f"{CODEBUDDY_BASE_URL}/v2/models",
        f"{CODEBUDDY_BASE_URL}/v1/models",
        f"{CODEBUDDY_BASE_URL}/models",
        f"{CODEBUDDY_BASE_URL}/api/models",
        f"{CODEBUDDY_BASE_URL}/v2/plugin/models",
        f"{CODEBUDDY_BASE_URL}/console/api/client/v1/models",
    ]

    for endpoint in endpoints:
        print(f"\n  Trying: {endpoint}")
        try:
            resp = await client.get(endpoint, headers=headers, timeout=15)
            print(f"    Status: {resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    print(f"    Response: {json.dumps(data, indent=2)[:2000]}")
                    # Try to extract model names
                    if isinstance(data, dict):
                        if "data" in data:
                            items = data["data"]
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict) and "id" in item:
                                        models_found.append(item["id"])
                                    elif isinstance(item, str):
                                        models_found.append(item)
                        if "models" in data:
                            items = data["models"]
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict) and "id" in item:
                                        models_found.append(item["id"])
                                    elif isinstance(item, str):
                                        models_found.append(item)
                except (json.JSONDecodeError, ValueError):
                    print(f"    Body: {resp.text[:500]}")
            elif resp.status_code != 404:
                print(f"    Body: {resp.text[:300]}")
        except Exception as e:
            print(f"    Error: {e}")

    return models_found


async def intercept_browser_traffic(email: str, password: str) -> str | None:
    """Login via browser and intercept network traffic to find model lists."""
    try:
        from browserforge.fingerprints import Screen
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("  [WARN] camoufox not installed, skipping browser intercept")
        return None

    print("\n" + "="*60)
    print("  BROWSER INTERCEPT MODE")
    print("  Logging in and capturing network traffic...")
    print("="*60)

    captured_requests = []
    found_models = []

    manager = AsyncCamoufox(
        headless=False,
        os="windows",
        block_webrtc=True,
        humanize=False,
        screen=Screen(max_width=1920, max_height=1080),
    )
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(30000)

    # Intercept all requests to codebuddy.ai
    async def on_response(response):
        url = response.url
        if "codebuddy.ai" not in url:
            return
        if any(ext in url for ext in [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico"]):
            return

        entry = {
            "url": url,
            "status": response.status,
            "method": response.request.method,
            "timestamp": time.time(),
        }

        # Capture response body for interesting endpoints
        if any(kw in url.lower() for kw in ["model", "chat", "completion", "config", "setting"]):
            try:
                body = await response.text()
                entry["body"] = body[:5000]
                # Look for model names in response
                if "model" in body.lower() or "gpt" in body.lower():
                    print(f"  [NET] [{response.request.method}] {url[:100]}")
                    print(f"      Status: {response.status}")
                    print(f"      Body: {body[:500]}")
                    # Try to parse and find models
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict):
                            _extract_models(data, found_models)
                    except (json.JSONDecodeError, ValueError):
                        pass
            except Exception:
                pass

        captured_requests.append(entry)

    page.on("response", on_response)

    # Navigate to CodeBuddy
    print("\n  Navigating to CodeBuddy...")
    await page.goto(f"{CODEBUDDY_BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    print("  Waiting for manual login or auto-login...")
    print("  (The browser is open — log in manually if needed)")
    print("  Press Ctrl+C when done exploring")

    try:
        # Wait indefinitely for user to explore
        while True:
            await asyncio.sleep(2)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    await manager.__aexit__(None, None, None)

    # Save captured data
    output = {
        "timestamp": time.time(),
        "requests_captured": len(captured_requests),
        "models_found": found_models,
        "requests": captured_requests[-100:],  # Last 100
    }

    output_file = "intercept_codebuddy_log.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  [SAVED] {len(captured_requests)} requests to {output_file}")
    print(f"  Models found: {found_models}")

    return None


def _extract_models(data: dict, found: list):
    """Recursively extract model names from a dict."""
    for key, value in data.items():
        if "model" in key.lower():
            if isinstance(value, str) and value:
                if value not in found:
                    found.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item not in found:
                        found.append(item)
                    elif isinstance(item, dict) and "id" in item:
                        if item["id"] not in found:
                            found.append(item["id"])
        if isinstance(value, dict):
            _extract_models(value, found)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _extract_models(item, found)


async def main():
    parser = argparse.ArgumentParser(description="Intercept CodeBuddy API for model discovery")
    parser.add_argument("--api-key", help="CodeBuddy API key (skip login)")
    parser.add_argument("--email", help="Account email (for browser login)")
    parser.add_argument("--password", help="Account password (for browser login)")
    parser.add_argument("--browser", action="store_true", help="Use browser intercept mode")
    parser.add_argument("--skip-known", action="store_true", help="Skip testing known models")
    args = parser.parse_args()

    api_key = args.api_key

    if not api_key and args.browser and args.email:
        await intercept_browser_traffic(args.email, args.password or "")
        return

    if not api_key:
        print("❌ --api-key is required (or use --browser with --email)")
        print("\nTo get an API key, check your CodeBuddy account or use:")
        print("  python intercept_codebuddy.py --browser --email you@gmail.com")
        sys.exit(1)

    headers = _build_headers(api_key)

    print("="*60)
    print("  CodeBuddy Model Discovery")
    print(f"  Endpoint: {CHAT_ENDPOINT}")
    print(f"  API Key: {api_key[:20]}...")
    print("="*60)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=60, write=30, pool=10),
        follow_redirects=True,
    ) as client:

        # Step 1: Try to list models
        print("\n\n" + "="*60)
        print("  STEP 1: Discovering model endpoints")
        print("="*60)
        discovered_models = await try_list_models(client, headers)
        if discovered_models:
            print(f"\n  [FOUND] Models discovered: {discovered_models}")

        # Step 2: Test GPT-5.5 variants
        print("\n\n" + "="*60)
        print("  STEP 2: Testing GPT-5.5 model name variants")
        print("="*60)

        results = []
        for model in GPT55_VARIANTS:
            result = await test_model(client, headers, model)
            results.append(result)
            await asyncio.sleep(1)  # Rate limit protection

        # Step 3: Test known models (validation)
        if not args.skip_known:
            print("\n\n" + "="*60)
            print("  STEP 3: Validating with known working models")
            print("="*60)

            for model in KNOWN_MODELS[:2]:  # Just test 2 known models
                result = await test_model(client, headers, model)
                results.append(result)
                await asyncio.sleep(1)

        # Summary
        print("\n\n" + "="*60)
        print("  SUMMARY")
        print("="*60)

        working = [r for r in results if r["status"] == 200]
        failed = [r for r in results if r["status"] and r["status"] >= 400]

        print(f"\n  [OK] Working models ({len(working)}):")
        for r in working:
            print(f"     {r['model']} -> response_model={r['response_model']}")

        print(f"\n  [FAIL] Failed models ({len(failed)}):")
        for r in failed:
            error_msg = ""
            if isinstance(r["error"], dict):
                error_msg = json.dumps(r["error"])[:200]
            else:
                error_msg = str(r["error"])[:200]
            print(f"     {r['model']} -> HTTP {r['status']}: {error_msg}")

        # Save full results
        output_file = "intercept_codebuddy_results.json"
        output = {
            "timestamp": time.time(),
            "endpoint": CHAT_ENDPOINT,
            "discovered_models": discovered_models,
            "test_results": results,
            "working_models": [{"model": r["model"], "response_model": r["response_model"]} for r in working],
            "failed_models": [{"model": r["model"], "status": r["status"], "error": str(r["error"])[:500]} for r in failed],
        }
        # Remove raw_chunks for cleaner output
        for r in output["test_results"]:
            r["raw_chunks"] = r["raw_chunks"][:3]  # Keep first 3 chunks only

        with open(output_file, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n  [SAVED] Full results saved to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
