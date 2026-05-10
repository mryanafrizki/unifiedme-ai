"""
Quick test script untuk verify setup tanpa menjalankan full automation
"""

import asyncio
from playwright.async_api import async_playwright
import sys


async def test_browser():
    """Test apakah Playwright browser bisa launch"""
    print("🧪 Testing Playwright browser setup...")
    
    try:
        async with async_playwright() as p:
            print("  ✅ Playwright imported successfully")
            
            browser = await p.chromium.launch(headless=False)
            print("  ✅ Chromium browser launched")
            
            page = await browser.new_page()
            print("  ✅ New page created")
            
            await page.goto('https://gumloop.com')
            print("  ✅ Navigated to gumloop.com")
            
            title = await page.title()
            print(f"  ✅ Page title: {title}")
            
            await asyncio.sleep(2)
            await browser.close()
            print("  ✅ Browser closed")
            
        print("\n✅ All tests passed! Setup is working correctly.")
        print("\nYou can now run the full automation:")
        print("  python run_automation.py")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure you ran: playwright install chromium")
        print("2. Check that all dependencies are installed: pip install -r requirements.txt")
        print("3. Try running setup.sh (Linux/Mac) or setup.bat (Windows)")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(test_browser())
