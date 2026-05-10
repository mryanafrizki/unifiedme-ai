#!/usr/bin/env python3

"""

Test Auto-Persistent Session Feature

Menggunakan requests langsung (tanpa library openai)

"""

import requests

import time

import json

import sys

# Konfigurasi

BASE_URL = "http://localhost:1430/v1"

MODEL = "gl-claude-sonnet-4-5"

HEADERS = {

    "Content-Type": "application/json",

    "Authorization": "Bearer sk-72c2da180b153171a997c4690f47271c503c812b96955be2"  # uncomment jika diperlukan

}

def chat(message: str, show_response: bool = True) -> str:

    """Kirim pesan menggunakan requests"""

    print(f"\n🔵 User: {message}")

    

    payload = {

        "model": MODEL,

        "messages": [

            {"role": "user", "content": message}

        ],

        # "chat_session_id": None   # sengaja tidak dikirim untuk test auto-persistent

    }

    

    try:

        response = requests.post(

            f"{BASE_URL}/chat/completions",

            headers=HEADERS,

            json=payload,

            timeout=60

        )

        

        if response.status_code != 200:

            print(f"❌ HTTP Error {response.status_code}: {response.text}")

            return f"ERROR: {response.status_code}"

        

        data = response.json()

        content = data["choices"][0]["message"]["content"].strip()

        

        if show_response:

            print(f"🤖 AI: {content}")

        return content

        

    except Exception as e:

        print(f"❌ Request Error: {e}")

        return f"ERROR: {e}"

def test_basic_memory():

    print("\n" + "="*70)

    print(" TEST 1: Basic Memory")

    print("="*70)

    

    chat("Hi! My name is Alice and I work as a Python developer.")

    time.sleep(1)

    

    response = chat("What is my name?")

    success = "Alice" in response or "alice" in response.lower()

    

    print("\n✅ PASSED" if success else "\n❌ FAILED")

    return success

def test_multi_turn_context():

    print("\n" + "="*70)

    print(" TEST 2: Multi-Turn Context")

    print("="*70)

    

    chat("I'm working on a FastAPI project.")

    time.sleep(1)

    chat("I'm using SQLAlchemy and PostgreSQL for the database.")

    time.sleep(1)

    

    response = chat("What tech stack am I using?")

    lower = response.lower()

    

    success = "fastapi" in lower and "sqlalchemy" in lower

    print("\n✅ PASSED" if success else "\n❌ FAILED")

    return success

def test_session_persistence():

    print("\n" + "="*70)

    print(" TEST 3: Session Persistence")

    print("="*70)

    

    chat("Remember: my favorite color is emerald green.")

    time.sleep(1)

    

    response = chat("What is my favorite color?")

    success = "green" in response.lower() or "emerald" in response.lower()

    

    print("\n✅ PASSED" if success else "\n❌ FAILED")

    return success

def main():

    print("\n" + "="*80)

    print("🚀 AUTO-PERSISTENT SESSION TEST (Pure requests)")

    print("="*80)

    

    tests = [

        ("Basic Memory", test_basic_memory),

        ("Multi-Turn Context", test_multi_turn_context),

        ("Session Persistence", test_session_persistence),

    ]

    

    results = []

    for name, func in tests:

        results.append((name, func()))

        input("\nTekan Enter untuk lanjut ke test berikutnya...")

    

    # Summary

    print("\n" + "="*80)

    print("📊 TEST SUMMARY")

    print("="*80)

    

    passed = sum(1 for _, r in results if r)

    for name, result in results:

        print(f"{'✅ PASS' if result else '❌ FAIL'}  {name}")

    

    print(f"\n📈 {passed}/{len(results)} test berhasil")

    

    if passed == len(results):

        print("\n🎉 SEMUA TEST BERHASIL!")

        print("Auto-persistent session berfungsi dengan baik.")

    else:

        print("\n⚠️ Ada test yang gagal. Cek proxy kamu.")

if __name__ == "__main__":

    main()