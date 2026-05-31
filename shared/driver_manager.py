"""Chrome WebDriver factory with anti-detection, headless support, and Linux compatibility."""

import os
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from shared.constants import DEFAULT_USER_AGENT
from shared.logging_config import setup_logger

logger = setup_logger("driver")


def get_driver(
    profile_dir: str = "chrome_profile",
    headless: bool = False,
    chrome_binary: str = "",
    user_agent: str = "",
    profile_base_dir: str = "",
):
    options = Options()

    # Profile path
    base = Path(profile_base_dir) if profile_base_dir else Path.cwd()
    profile_path = base / profile_dir
    options.add_argument(f"--user-data-dir={profile_path}")

    # Headless mode (Linux servers)
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    # Chrome binary path (Linux)
    if chrome_binary:
        options.binary_location = chrome_binary

    # Anti-detection
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    # Performance & stability
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")
    options.add_argument("--remote-debugging-port=0")

    # User-Agent
    ua = user_agent or DEFAULT_USER_AGENT
    options.add_argument(f"--user-agent={ua}")

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info(f"Chrome driver created: profile={profile_dir}, headless={headless}")
        return driver
    except Exception as e:
        logger.error(f"Failed to create Chrome driver: {e}")
        raise
