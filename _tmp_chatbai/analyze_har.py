#!/usr/bin/env python3
"""Quick analysis of captured HAR log."""
import json

with open("_tmp_chatbai/intercept_chatbai_log.json", encoding="utf-8") as f:
    data = json.load(f)

reqs = data["http_requests"]
ws = data["ws_frames"]
print(f"Total HTTP requests: {len(reqs)}")
print(f"Total WS frames: {len(ws)}")

print("\n=== NON-GET REQUESTS ===")
for r in reqs:
    if r["method"] != "GET":
        print(f"\n{r['method']} {r['url'][:140]}")
        print(f"  Status: {r['status']}")
        if r.get("request_body"):
            print(f"  REQ: {str(r['request_body'])[:300]}")
        if r.get("response_body"):
            print(f"  RES: {str(r['response_body'])[:300]}")

print("\n=== KEY GET REQUESTS (API calls) ===")
for r in reqs:
    if r["method"] == "GET" and "/api/" in r["url"] or "/webapi/" in r["url"] or "/auth/" in r["url"]:
        print(f"\nGET {r['url'][:140]}")
        print(f"  Status: {r['status']}")
        if r.get("response_body"):
            print(f"  RES: {str(r['response_body'])[:300]}")

print("\n=== COOKIES SET ===")
for r in reqs:
    sc = r.get("response_headers", {}).get("set-cookie", "")
    if sc and "authjs" in sc.lower() or "session" in sc.lower() or "token" in sc.lower():
        print(f"\n{r['url'][:100]}")
        for line in sc.split("\n"):
            name = line.split("=")[0].strip()
            print(f"  {name}")

if ws:
    print(f"\n=== WEBSOCKET FRAMES ({len(ws)}) ===")
    for frame in ws[:20]:
        print(f"  [{frame['direction']}] {frame['url'][:60]}: {frame['payload'][:150]}")
