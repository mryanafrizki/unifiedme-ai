"""
Gumloop University Automation Script
Automates the flow: Account creation -> MCP binding -> University quiz completion
"""

import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright, Page, Browser
from typing import List, Dict, Optional
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('gumloop_university_automation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class GumloopUniversityAutomation:
    """Automates Gumloop account creation, MCP binding, and University quiz completion"""
    
    def __init__(
        self,
        email: str,
        password: str,
        mcp_server_config: Dict,
        quiz_answers: List[str],
        headless: bool = False,
        screenshot_dir: str = "screenshots"
    ):
        """
        Args:
            email: Email untuk account Gumloop
            password: Password untuk account Gumloop
            mcp_server_config: Config MCP server (name, endpoint, etc)
            quiz_answers: List of 6 answers untuk quiz University
            headless: Run browser in headless mode
            screenshot_dir: Directory untuk simpan screenshots
        """
        self.email = email
        self.password = password
        self.mcp_server_config = mcp_server_config
        self.quiz_answers = quiz_answers
        self.headless = headless
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(exist_ok=True)
        
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.context = None
        
    async def take_screenshot(self, name: str):
        """Take screenshot dengan timestamp"""
        if self.page:
            timestamp = asyncio.get_event_loop().time()
            filepath = self.screenshot_dir / f"{timestamp}_{name}.png"
            await self.page.screenshot(path=str(filepath), full_page=True)
            logger.info(f"Screenshot saved: {filepath}")
            
    async def setup_browser(self):
        """Initialize Playwright browser"""
        logger.info("Setting up browser...")
        playwright = await async_playwright().start()
        
        self.browser = await playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
        )
        
        # Create context dengan persistent storage untuk cookies/local storage
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        self.page = await self.context.new_page()
        
        # Enable request/response interception untuk logging
        await self.page.route("**/*", self._intercept_request)
        self.page.on("response", self._intercept_response)
        
        logger.info("Browser setup complete")
        
    async def _intercept_request(self, route):
        """Intercept dan log requests"""
        request = route.request
        
        # Log important requests
        if any(keyword in request.url for keyword in ['api', 'oauth', 'authorize', 'login', 'mcp']):
            logger.info(f"REQUEST: {request.method} {request.url}")
            if request.post_data:
                try:
                    logger.info(f"  POST DATA: {request.post_data}")
                except:
                    pass
                    
        await route.continue_()
        
    async def _intercept_response(self, response):
        """Intercept dan log responses"""
        # Log important responses
        if any(keyword in response.url for keyword in ['api', 'oauth', 'authorize', 'login', 'mcp']):
            logger.info(f"RESPONSE: {response.status} {response.url}")
            
    async def create_account(self):
        """Create new Gumloop account"""
        logger.info(f"Creating account for {self.email}...")
        
        await self.page.goto('https://gumloop.com/signup')
        await self.take_screenshot("01_signup_page")
        
        # Fill signup form
        await self.page.fill('input[type="email"]', self.email)
        await self.page.fill('input[type="password"]', self.password)
        await self.take_screenshot("02_signup_filled")
        
        # Submit
        await self.page.click('button[type="submit"]')
        await self.page.wait_for_load_state('networkidle')
        await self.take_screenshot("03_signup_complete")
        
        logger.info("Account creation complete")
        
    async def login(self):
        """Login to existing account"""
        logger.info(f"Logging in as {self.email}...")
        
        await self.page.goto('https://gumloop.com/login')
        await self.take_screenshot("04_login_page")
        
        # Fill login form
        await self.page.fill('input[type="email"]', self.email)
        await self.page.fill('input[type="password"]', self.password)
        await self.take_screenshot("05_login_filled")
        
        # Submit
        await self.page.click('button[type="submit"]')
        await self.page.wait_for_load_state('networkidle')
        await self.take_screenshot("06_login_complete")
        
        logger.info("Login complete")
        
    async def add_mcp_server(self):
        """Add MCP server to account"""
        logger.info(f"Adding MCP server: {self.mcp_server_config.get('name')}...")
        
        # Navigate to MCP settings (adjust URL based on actual Gumloop UI)
        await self.page.goto('https://gumloop.com/settings/mcp')
        await self.take_screenshot("07_mcp_settings")
        
        # Click "Add MCP Server" button
        await self.page.click('text=Add MCP Server')
        await self.page.wait_for_timeout(1000)
        await self.take_screenshot("08_mcp_add_dialog")
        
        # Fill MCP config
        if 'name' in self.mcp_server_config:
            await self.page.fill('input[name="name"]', self.mcp_server_config['name'])
        if 'endpoint' in self.mcp_server_config:
            await self.page.fill('input[name="endpoint"]', self.mcp_server_config['endpoint'])
        if 'description' in self.mcp_server_config:
            await self.page.fill('textarea[name="description"]', self.mcp_server_config['description'])
            
        await self.take_screenshot("09_mcp_filled")
        
        # Submit
        await self.page.click('button:has-text("Add"), button:has-text("Save")')
        await self.page.wait_for_load_state('networkidle')
        await self.take_screenshot("10_mcp_added")
        
        logger.info("MCP server added successfully")
        
    async def navigate_to_university(self):
        """Navigate to Gumloop University"""
        logger.info("Navigating to Gumloop University...")
        
        await self.page.goto('https://university.gumloop.com/getting-started-with-gumloop/what-is-gumloop')
        await self.page.wait_for_load_state('networkidle')
        await self.take_screenshot("11_university_landing")
        
        # Check if we're redirected to OAuth
        current_url = self.page.url
        if 'oauth/authorize' in current_url:
            logger.info("Redirected to OAuth authorize page")
            await self.handle_oauth_authorization()
        else:
            logger.info("No OAuth redirect detected yet")
            
    async def handle_oauth_authorization(self):
        """Handle OAuth authorization flow"""
        logger.info("Handling OAuth authorization...")
        
        await self.take_screenshot("12_oauth_authorize")
        
        # Wait for "Allow" button and click it
        try:
            # Try different possible selectors for Allow button
            allow_selectors = [
                'button:has-text("Allow")',
                'button:has-text("Authorize")',
                'button[type="submit"]:has-text("Allow")',
                'input[type="submit"][value="Allow"]',
                '[data-testid="allow-button"]',
            ]
            
            for selector in allow_selectors:
                try:
                    await self.page.wait_for_selector(selector, timeout=5000)
                    await self.page.click(selector)
                    logger.info(f"Clicked Allow button: {selector}")
                    break
                except:
                    continue
                    
            await self.page.wait_for_load_state('networkidle')
            await self.take_screenshot("13_oauth_allowed")
            
            logger.info("OAuth authorization complete")
            
        except Exception as e:
            logger.error(f"Failed to click Allow button: {e}")
            await self.take_screenshot("13_oauth_error")
            raise
            
    async def complete_university_quiz(self):
        """Complete the University quiz with provided answers"""
        logger.info("Starting University quiz...")
        
        await self.page.wait_for_timeout(2000)  # Wait for quiz to load
        await self.take_screenshot("14_quiz_start")
        
        for idx, answer in enumerate(self.quiz_answers, start=1):
            logger.info(f"Answering question {idx}: {answer}")
            
            # Wait for question to appear
            await self.page.wait_for_selector(f'[data-question="{idx}"], .question-{idx}, .quiz-question', timeout=10000)
            await self.take_screenshot(f"15_question_{idx}")
            
            # Find and click/fill the answer
            # This depends on question type (multiple choice, text input, etc.)
            # Try multiple strategies:
            
            # Strategy 1: Radio button / checkbox
            try:
                await self.page.click(f'input[value="{answer}"]')
                logger.info(f"  Clicked radio/checkbox for: {answer}")
            except:
                pass
                
            # Strategy 2: Button with text
            try:
                await self.page.click(f'button:has-text("{answer}")')
                logger.info(f"  Clicked button: {answer}")
            except:
                pass
                
            # Strategy 3: Text input
            try:
                text_input = await self.page.query_selector('input[type="text"], textarea')
                if text_input:
                    await text_input.fill(answer)
                    logger.info(f"  Filled text input: {answer}")
            except:
                pass
                
            await self.take_screenshot(f"16_question_{idx}_answered")
            
            # Click "Next" or "Submit" button
            try:
                next_selectors = [
                    'button:has-text("Next")',
                    'button:has-text("Continue")',
                    'button:has-text("Submit")',
                    '[data-testid="next-button"]',
                ]
                
                for selector in next_selectors:
                    try:
                        await self.page.wait_for_selector(selector, timeout=3000)
                        await self.page.click(selector)
                        logger.info(f"  Clicked next button: {selector}")
                        break
                    except:
                        continue
                        
                await self.page.wait_for_load_state('networkidle')
                await self.page.wait_for_timeout(1000)
                
            except Exception as e:
                logger.warning(f"  Could not find next button: {e}")
                
        await self.take_screenshot("17_quiz_complete")
        logger.info("Quiz completed!")
        
    async def run_full_flow(self, skip_account_creation: bool = False):
        """Run the complete automation flow"""
        try:
            await self.setup_browser()
            
            if not skip_account_creation:
                await self.create_account()
            else:
                await self.login()
                
            await self.add_mcp_server()
            await self.navigate_to_university()
            
            # Check if we need to handle OAuth
            if 'oauth/authorize' in self.page.url:
                await self.handle_oauth_authorization()
                
            # Wait a bit for redirect to complete
            await self.page.wait_for_timeout(3000)
            
            # Complete the quiz
            await self.complete_university_quiz()
            
            logger.info("✅ Full automation flow completed successfully!")
            
            # Keep browser open for manual inspection
            if not self.headless:
                logger.info("Browser kept open for inspection. Press Ctrl+C to close.")
                await asyncio.sleep(3600)  # Keep alive for 1 hour
                
        except Exception as e:
            logger.error(f"❌ Automation failed: {e}")
            await self.take_screenshot("error_final")
            raise
            
        finally:
            if self.browser:
                await self.browser.close()
                logger.info("Browser closed")
                

async def main():
    """Example usage"""
    
    # Configuration
    config = {
        "email": "your-email@example.com",  # CHANGE THIS
        "password": "your-password",  # CHANGE THIS
        "mcp_server_config": {
            "name": "My Custom MCP Server",
            "endpoint": "http://localhost:3000",
            "description": "Custom MCP server for testing"
        },
        "quiz_answers": [
            "Answer for question 1",  # CHANGE THIS
            "Answer for question 2",  # CHANGE THIS
            "Answer for question 3",  # CHANGE THIS
            "Answer for question 4",  # CHANGE THIS
            "Answer for question 5",  # CHANGE THIS
            "Answer for question 6",  # CHANGE THIS
        ],
        "headless": False,  # Set True untuk run tanpa GUI
        "skip_account_creation": False,  # Set True jika account sudah ada
    }
    
    # Validate quiz answers
    if len(config["quiz_answers"]) != 6:
        raise ValueError("Quiz answers must have exactly 6 items!")
        
    # Run automation
    automation = GumloopUniversityAutomation(
        email=config["email"],
        password=config["password"],
        mcp_server_config=config["mcp_server_config"],
        quiz_answers=config["quiz_answers"],
        headless=config["headless"],
    )
    
    await automation.run_full_flow(skip_account_creation=config["skip_account_creation"])


if __name__ == "__main__":
    asyncio.run(main())
