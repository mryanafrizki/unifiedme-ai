"""
Production Configuration
Centralized settings for Gumloop University automation
"""

# Production Mode Settings
PRODUCTION_MODE = True
SAVE_SCREENSHOTS = False  # Disable screenshot saving in production
SAVE_REQUEST_LOG = False  # Disable HTTP request logging
SAVE_RESULT_JSON = True   # Keep result.json for credentials

# Output Paths
RESULT_JSON_PATH = "result.json"  # Credentials output
SCREENSHOT_DIR = None             # Disabled in production
LOG_FILE_PATH = None              # Disabled in production

# Browser Settings
HEADLESS_MODE = False  # Set True for server environments
BROWSER_TIMEOUT = 30000  # 30 seconds
PAGE_TIMEOUT = 15000     # 15 seconds

# University Settings
UNIVERSITY_BASE = "https://university.gumloop.com"
API_BASE = "https://api.gumloop.com"

# Course Configuration
COURSES_ENABLED = ["getting-started-with-gumloop", "ai-fundamentals"]

# Retry Settings
MAX_LOGIN_RETRIES = 3
TOKEN_EXTRACTION_RETRIES = 8
TOKEN_RETRY_INTERVAL = 3  # seconds

# Logging
VERBOSE_LOGGING = True  # Console output only
LOG_LEVEL = "INFO"      # INFO, DEBUG, WARNING, ERROR
