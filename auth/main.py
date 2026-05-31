"""Auth service — standalone process that captures and serves JWT/cookies.

Runs 2 Chrome profiles (G2G + Eldorado), captures auth via CDP,
serves to other processes via HTTP API.

Usage:
    python -m auth.main

Endpoints:
    GET /auth/g2g     → {jwt_token, cookies, user_agent, seller_id}
    GET /auth/eldo    → {cookies, xsrf_token, user_agent}
    GET /health       → {status, uptime, jwt_expires_in}
"""

import asyncio
import base64
import json
import os
import signal
import threading
import time
import logging
from pathlib import Path

from aiohttp import web
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from shared.config import CHROME_BINARY_PATH, HEADLESS_MODE, DATABASE_PATH, G2G_EMAIL, G2G_PASSWORD
from shared.constants import DEFAULT_USER_AGENT
from shared.database import Database
from shared.logging_config import setup_logger

logger = setup_logger("auth.service")


# ── Login State (cross-thread OTP relay) ──

class LoginState:
    """Shared state between capture thread and HTTP handlers for OTP relay."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"          # idle | logging_in | need_otp | success | failed
        self.message = ""
        self._otp_code = ""
        self._otp_event = threading.Event()

    def set_need_otp(self):
        with self._lock:
            self.status = "need_otp"
            self.message = "OTP required — enter code on dashboard"
            self._otp_event.clear()

    def submit_otp(self, code: str):
        with self._lock:
            self._otp_code = code
            self.status = "logging_in"
            self.message = "OTP received, continuing login..."
            self._otp_event.set()

    def set_result(self, success: bool, msg: str = ""):
        with self._lock:
            self.status = "success" if success else "failed"
            self.message = msg

    def reset(self):
        with self._lock:
            self.status = "idle"
            self.message = ""
            self._otp_code = ""
            self._otp_event.clear()

    def wait_for_otp(self, timeout: float = 300) -> str:
        self._otp_event.wait(timeout=timeout)
        with self._lock:
            return self._otp_code

    def to_dict(self) -> dict:
        with self._lock:
            return {"status": self.status, "message": self.message}


login_state = LoginState()

# Profile status tracking
profile_status = {}  # {"chrome_profile_g2g": {"alive": True, "checked_at": ..., "detail": "..."}, ...}

AUTH_PORT = int(os.getenv("AUTH_PORT", "8010"))

G2G_HOME = "https://www.g2g.com"
G2G_DASHBOARD = "https://www.g2g.com/g2g-user/sale?status=preparing"
G2G_LOGIN = "https://www.g2g.com/login"
ELDO_HOME = "https://www.eldorado.gg"

G2G_PROFILES = ["chrome_profile_g2g"]
ELDO_PROFILES = ["chrome_profile_eldo", "chrome_profile_eldo_bak1", "chrome_profile_eldo_bak2"]

JWT_TTL = 780  # 13 min — refresh before JWT expires (15 min)


def _jwt_exp(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _create_driver(profile_dir: str):
    """Create Chrome driver with performance logging for CDP capture."""
    options = Options()
    profile_path = Path.cwd() / profile_dir
    options.add_argument(f"--user-data-dir={profile_path}")

    if HEADLESS_MODE:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    if CHROME_BINARY_PATH:
        options.binary_location = CHROME_BINARY_PATH

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_argument(f"--user-agent={DEFAULT_USER_AGENT}")

    # CDP performance logging for JWT capture
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


class PlatformAuth:
    """Base class for platform auth capture."""

    def __init__(self, profile_dir: str, platform: str):
        self.profile_dir = profile_dir
        self.platform = platform
        self.driver = None
        self.data = None
        self.captured_at = 0

    def init_driver(self):
        if self.driver:
            try:
                self.driver.current_url
                return True
            except Exception:
                self.driver = None

        try:
            self.driver = _create_driver(self.profile_dir)
            return True
        except Exception as e:
            logger.error("[%s] Driver creation failed: %s", self.platform, e)
            return False

    def _extract_jwt_from_logs(self) -> str:
        """Parse CDP performance logs for the LATEST valid JWT."""
        try:
            logs = self.driver.get_log("performance")
        except Exception:
            return ""

        now = time.time()
        best_token = ""
        best_exp = 0

        for entry in logs:
            try:
                log_data = json.loads(entry.get("message", "{}"))
                message = log_data.get("message", {})
                method = message.get("method", "")

                if "Network.requestWillBeSent" in method:
                    request = message.get("params", {}).get("request", {})
                    url = request.get("url", "")
                    if "sls.g2g.com" in url or "eldorado.gg" in url:
                        headers = request.get("headers", {})
                        auth = headers.get("Authorization") or headers.get("authorization", "")
                        if auth:
                            token = auth.replace("Bearer ", "").strip()
                            if token and len(token) > 50:
                                exp = _jwt_exp(token)
                                if exp and exp > best_exp:
                                    best_token = token
                                    best_exp = exp
            except Exception:
                continue

        # Only return if token hasn't expired
        if best_token and best_exp > now:
            return best_token
        return best_token if best_token else ""

    def _extract_cookies(self) -> dict:
        try:
            return {c["name"]: c["value"] for c in self.driver.get_cookies()}
        except Exception:
            return {}

    def _extract_jwt_from_storage(self) -> str:
        """Fallback: extract JWT from localStorage/sessionStorage."""
        try:
            return self.driver.execute_script("""
                for (let i = 0; i < localStorage.length; i++) {
                    const val = localStorage.getItem(localStorage.key(i));
                    if (val && typeof val === 'string' && val.startsWith('eyJ') && val.length > 100)
                        return val;
                }
                for (let i = 0; i < sessionStorage.length; i++) {
                    const val = sessionStorage.getItem(sessionStorage.key(i));
                    if (val && typeof val === 'string' && val.startsWith('eyJ') && val.length > 100)
                        return val;
                }
                return "";
            """) or ""
        except Exception:
            return ""

    def is_fresh(self) -> bool:
        if self.data is None:
            return False
        # Check actual JWT expiry, not just capture time
        jwt = self.data.get("jwt_token") if self.platform == "g2g" else None
        if jwt:
            exp = _jwt_exp(jwt)
            if exp and exp - time.time() < 120:  # Less than 2 min remaining
                return False
        return time.time() - self.captured_at < JWT_TTL

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


class G2GAuth(PlatformAuth):
    def __init__(self):
        super().__init__(G2G_PROFILES[0], "g2g")
        self._consecutive_failures = 0
        self._last_failure_time = 0
        self._captcha_until = 0  # timestamp: skip auto-login until this time

    def _auto_login(self) -> bool:
        """Attempt auto-login when session expired. Returns True if login succeeded."""
        if not G2G_EMAIL or not G2G_PASSWORD:
            login_state.set_result(False, "G2G_EMAIL/G2G_PASSWORD not configured")
            logger.error("[G2G] Auto-login skipped: credentials not configured")
            return False

        import time as _time
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import (
            StaleElementReferenceException, NoSuchElementException,
            WebDriverException, TimeoutException,
        )

        # Don't reset — just update status directly
        login_state.status = "logging_in"
        login_state.message = "Auto-login in progress..."
        logger.info("[G2G] Starting auto-login for %s", G2G_EMAIL)

        def _safe_clear_send_keys(element, text):
            """Clear + send_keys with retry for stale elements."""
            try:
                element.clear()
                element.send_keys(text)
                return True
            except (StaleElementReferenceException, WebDriverException):
                return False

        try:
            # Navigate to login page
            self.driver.get(G2G_LOGIN)
            _time.sleep(3)

            # Fill email — G2G uses type="text" with random IDs
            email_selectors = [
                (By.CSS_SELECTOR, 'input[type="text"]'),
                (By.CSS_SELECTOR, 'input[name="email"]'),
                (By.CSS_SELECTOR, 'input[type="email"]'),
            ]
            filled = False
            for method, sel in email_selectors:
                try:
                    el = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((method, sel)))
                    if _safe_clear_send_keys(el, G2G_EMAIL):
                        filled = True
                        break
                except (TimeoutException, WebDriverException):
                    continue
            if not filled:
                login_state.set_result(False, "Email field not found")
                logger.error("[G2G] Auto-login: email field not found")
                return False

            # Fill password
            pwd_selectors = [
                (By.CSS_SELECTOR, 'input[type="password"]'),
                (By.CSS_SELECTOR, 'input[name="password"]'),
            ]
            filled = False
            for method, sel in pwd_selectors:
                try:
                    el = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((method, sel)))
                    if _safe_clear_send_keys(el, G2G_PASSWORD):
                        filled = True
                        break
                except (TimeoutException, WebDriverException):
                    continue
            if not filled:
                login_state.set_result(False, "Password field not found")
                logger.error("[G2G] Auto-login: password field not found")
                return False

            # Click submit
            submit_selectors = [
                (By.CSS_SELECTOR, 'button[type="submit"]'),
                (By.XPATH, '//button[contains(text(), "Sign In")]'),
                (By.XPATH, '//button[contains(text(), "Login")]'),
            ]
            clicked = False
            for method, sel in submit_selectors:
                try:
                    el = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable((method, sel)))
                    el.click()
                    clicked = True
                    break
                except (TimeoutException, WebDriverException):
                    continue
            if not clicked:
                login_state.set_result(False, "Submit button not found")
                logger.error("[G2G] Auto-login: submit button not found")
                return False

            _time.sleep(5)

            # Check if OTP is required — may need extra wait for form to render
            current_url = self.driver.current_url.lower()
            logger.info("[G2G] After submit URL: %s", self.driver.current_url[:100])

            if "login" in current_url or "otp" in current_url or "verify" in current_url:
                # Wait up to 15s for OTP form to appear
                otp_inputs = []
                for otp_wait in range(3):
                    otp_inputs = self.driver.find_elements(By.CSS_SELECTOR, 'input[maxlength="1"]')
                    if not otp_inputs:
                        otp_inputs = self.driver.find_elements(By.CSS_SELECTOR,
                            'input[name*="otp"], input[name*="code"], input[id*="otp"]')
                    if otp_inputs:
                        break
                    logger.info("[G2G] OTP inputs not found yet, waiting... (%d/3)", otp_wait + 1)
                    _time.sleep(5)

                # Check for captcha → open VNC for manual login
                captcha = self.driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="recaptcha"], iframe[src*="captcha"], div.g-recaptcha, div[class*="captcha"]')
                if captcha:
                    logger.warning("[G2G] Captcha detected — opening VNC for manual login")
                    login_state.set_result("need_manual", "Captcha detected — please login via VNC")

                    # Close headless Chrome to free profile
                    self.close()

                    # Open Chrome on VNC display
                    import subprocess
                    subprocess.Popen([
                        "google-chrome-stable",
                        "--no-sandbox", "--disable-dev-shm-usage",
                        f"--user-data-dir={Path.cwd() / self.profile_dir}",
                        G2G_LOGIN,
                    ], env={**os.environ, "DISPLAY": ":99"})
                    logger.info("[G2G] VNC Chrome opened — waiting for manual login (5min)")

                    # Poll: wait until session becomes valid
                    for _ in range(60):  # 5 min = 60 x 5s
                        _time.sleep(5)
                        if not self.init_driver():
                            continue
                        try:
                            self.driver.get(G2G_DASHBOARD)
                            _time.sleep(5)
                            if "login" not in self.driver.current_url.lower():
                                logger.info("[G2G] Manual login successful!")
                                login_state.set_result(True, "Manual login successful")
                                return True
                        except Exception:
                            pass
                        self.close()

                    logger.error("[G2G] Manual login timeout")
                    login_state.set_result(False, "Manual login timeout — 5 min exceeded")
                    return False

                logger.info("[G2G] OTP inputs found: %d", len(otp_inputs))

                if otp_inputs:
                    logger.info("[G2G] OTP required — waiting for dashboard input (5min timeout)")
                    login_state.set_need_otp()
                    otp_code = login_state.wait_for_otp(timeout=300)

                    if not otp_code:
                        login_state.set_result(False, "OTP timeout")
                        logger.error("[G2G] OTP timeout — no code received")
                        return False

                    # Re-find OTP inputs (stale after waiting)
                    otp_inputs = self.driver.find_elements(By.CSS_SELECTOR, 'input[maxlength="1"]')
                    if not otp_inputs:
                        otp_inputs = self.driver.find_elements(By.CSS_SELECTOR,
                            'input[name*="otp"], input[name*="code"], input[id*="otp"]')

                    for i, digit in enumerate(otp_code[:len(otp_inputs)]):
                        try:
                            otp_inputs[i].send_keys(digit)
                        except (StaleElementReferenceException, WebDriverException):
                            logger.warning("[G2G] OTP input %d stale, skipping", i)

                    _time.sleep(2)

                    verify_selectors = [
                        (By.XPATH, '//button[contains(text(), "Verify")]'),
                        (By.XPATH, '//button[contains(text(), "Submit")]'),
                        (By.XPATH, '//button[contains(text(), "Confirm")]'),
                        (By.CSS_SELECTOR, 'button[type="submit"]'),
                    ]
                    for method, sel in verify_selectors:
                        try:
                            el = WebDriverWait(self.driver, 3).until(
                                EC.element_to_be_clickable((method, sel)))
                            el.click()
                            break
                        except (TimeoutException, WebDriverException):
                            continue

                    _time.sleep(5)

            # Check login result
            final_url = self.driver.current_url.lower()
            if "login" not in final_url:
                logger.info("[G2G] Auto-login successful! URL: %s", final_url[:60])
                login_state.set_result(True, "Login successful")
                return True
            else:
                # Log page state for debugging
                try:
                    page_src = self.driver.page_source[:500]
                    logger.error("[G2G] Auto-login failed — still on login page. Page snippet: %s", page_src)
                except Exception:
                    pass
                login_state.set_result(False, "Still on login page after auto-login")
                return False

        except WebDriverException as e:
            login_state.set_result(False, "Chrome session lost — retry later")
            logger.error("[G2G] Auto-login driver error: %s", e)
            return False
        except Exception as e:
            login_state.set_result(False, f"Auto-login error: {e}")
            logger.error("[G2G] Auto-login error: %s", e)
            return False

    def capture(self) -> dict:
        # Back off if capture keeps failing — avoid Chrome instance exhaustion
        if self._consecutive_failures >= 3:
            since_last = time.time() - self._last_failure_time
            if since_last < 300:
                logger.warning("[G2G] Skipping capture — cooling down (%d failures, %.0fs ago)",
                               self._consecutive_failures, since_last)
                return self.data or {}
            else:
                # Cooldown expired — reset and try again
                logger.info("[G2G] Cooldown expired, resetting failures and retrying")
                self._consecutive_failures = 0

        import time as _time

        # Close and recreate driver to clear stale CDP logs
        self.close()
        if not self.init_driver():
            return {}

        try:
            self.driver.get(G2G_DASHBOARD)
        except Exception:
            try:
                self.driver.get(G2G_HOME)
            except Exception as e:
                logger.error("[G2G] Navigation failed: %s", e)
                self.close()
                self._consecutive_failures += 1
                self._last_failure_time = time.time()
                return {}

        _time.sleep(8)

        # Check if redirected to login (session expired)
        current_url = self.driver.current_url
        if "login" in current_url.lower():
            logger.warning("[G2G] Session expired — attempting auto-login")

            # Skip if captcha cooldown
            if time.time() < self._captcha_until:
                logger.warning("[G2G] Skipping auto-login — captcha cooldown (%.0fs left)",
                               self._captcha_until - time.time())
                self.close()
                return self.data or {}

            if self._auto_login():
                try:
                    self.driver.get(G2G_DASHBOARD)
                    _time.sleep(5)
                except Exception:
                    pass
            else:
                self.close()
                self._consecutive_failures += 1
                self._last_failure_time = time.time()
                return self.data or {}

        jwt = ""
        for attempt in range(3):
            jwt = self._extract_jwt_from_logs()
            if jwt:
                break
            jwt = self._extract_jwt_from_storage()
            if jwt:
                break
            if attempt < 2:
                logger.info("[G2G] JWT not found, reloading (attempt %d)...", attempt + 2)
                self.driver.refresh()
                _time.sleep(5)

        if not jwt:
            logger.error("[G2G] JWT capture failed")
            self.close()
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            return {}

        # Validate JWT hasn't expired
        exp = _jwt_exp(jwt)
        if exp and exp < time.time():
            logger.error("[G2G] JWT expired")
            self.close()
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            return {}

        cookies = self._extract_cookies()
        self.data = {
            "jwt_token": jwt,
            "cookies": cookies,
            "user_agent": DEFAULT_USER_AGENT,
        }
        self.captured_at = time.time()

        short = jwt[:20] + "..."
        logger.info("[G2G] JWT captured: %s | cookies: %d | exp: %s",
                     short, len(cookies),
                     f"{(exp - time.time()) / 60:.0f}min" if exp else "unknown")
        self._consecutive_failures = 0
        return self.data


class EldoAuth(PlatformAuth):
    """Eldorado auth using Camoufox (bypass Cloudflare Turnstile)."""

    def __init__(self):
        super().__init__(ELDO_PROFILES[0], "eldo")
        self._profile_idx = 0
        self._consecutive_failures = 0
        self._last_failure_time = 0

    def _next_profile(self):
        self._profile_idx = (self._profile_idx + 1) % len(ELDO_PROFILES)
        self.profile_dir = ELDO_PROFILES[self._profile_idx]
        logger.info("[ELDO] Switching to profile: %s", self.profile_dir)

    def _capture_single(self, profile_dir: str) -> dict:
        """Capture auth from a single profile. Returns data or {}."""
        import time as _time
        from pathlib import Path

        # Isolate from main thread's asyncio event loop — Playwright sync API
        # refuses to run if it detects a running event loop, and on Python <3.12
        # asyncio.get_event_loop() leaks the main thread's loop into worker threads.
        asyncio.set_event_loop(asyncio.new_event_loop())

        profile_path = Path.cwd() / profile_dir
        profile_path.mkdir(parents=True, exist_ok=True)

        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            logger.error("[ELDO] Camoufox not installed.")
            return {}

        logger.info("[ELDO] Opening Camoufox for profile: %s", profile_dir)
        try:
            with Camoufox(
                headless=True,
                humanize=True,
                window=(1280, 720),
                persistent_context=True,
                user_data_dir=str(profile_path),
            ) as browser:
                page = browser.new_page()
                page.goto(ELDO_HOME, timeout=60000)
                _time.sleep(5)

                # Handle Cloudflare Turnstile
                try:
                    turnstile = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                    if turnstile:
                        turnstile.locator("body").click()
                        _time.sleep(3)
                        logger.info("[ELDO] Clicked Turnstile CAPTCHA")
                except Exception:
                    pass

                _time.sleep(5)

                # Trigger XSRF cookie
                try:
                    page.evaluate("""
                        fetch('/api/authentication/claims', {credentials: 'include'})
                        .then(r => r.json()).catch(() => {})
                    """)
                    _time.sleep(2)
                except Exception:
                    pass

                # Extract cookies
                cookies_list = page.context.cookies()
                cookies = {c["name"]: c["value"] for c in cookies_list}
                xsrf = cookies.get("XSRF-TOKEN", "")

                if not xsrf:
                    for name in cookies:
                        nl = name.lower()
                        if "xsrf" in nl or "csrf" in nl or "antiforgery" in nl:
                            xsrf = cookies[name]
                            logger.info("[ELDO] Found XSRF-like cookie: %s", name)
                            break

                url = page.url
                logged_in = "login" not in url.lower()

                data = {
                    "cookies": cookies,
                    "xsrf_token": xsrf,
                    "user_agent": DEFAULT_USER_AGENT,
                    "logged_in": logged_in,
                }
                logger.info("[ELDO] Capture: cookies=%d | xsrf=%s | logged_in=%s | url=%s | profile=%s",
                             len(cookies), "yes" if xsrf else "no", logged_in, url[:60], profile_dir)

                if not logged_in:
                    logger.warning("[ELDO] Not logged in on profile: %s", profile_dir)
                return data

        except Exception as e:
            logger.error("[ELDO] Camoufox capture failed on %s: %s", profile_dir, e)
            return {}

    def capture(self) -> dict:
        # Back off if capture keeps failing
        if self._consecutive_failures >= 3:
            since_last = time.time() - self._last_failure_time
            if since_last < 300:
                logger.warning("[ELDO] Skipping capture — cooling down (%d failures, %.0fs ago)",
                               self._consecutive_failures, since_last)
                return self.data or {}

        for i in range(len(ELDO_PROFILES)):
            profile = ELDO_PROFILES[(self._profile_idx + i) % len(ELDO_PROFILES)]
            data = self._capture_single(profile)
            if data and data.get("logged_in") and data.get("xsrf_token"):
                self.data = data
                self.captured_at = time.time()
                self.profile_dir = profile
                self._profile_idx = ELDO_PROFILES.index(profile)
                self._consecutive_failures = 0
                return data
            logger.warning("[ELDO] Profile %s failed, trying next...", profile)
            self._next_profile()

        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        logger.error("[ELDO] All %d profiles exhausted", len(ELDO_PROFILES))
        return self.data or {}

        logger.info("[ELDO] Opening Camoufox for cookie capture...")
        try:
            with Camoufox(
                headless=True,
                humanize=True,
                window=(1280, 720),
                persistent_context=True,
                user_data_dir=str(profile_path),
            ) as browser:
                page = browser.new_page()
                page.goto(ELDO_HOME, timeout=60000)
                _time.sleep(5)

                # Handle Cloudflare Turnstile
                try:
                    turnstile = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                    if turnstile:
                        turnstile.locator("body").click()
                        _time.sleep(3)
                        logger.info("[ELDO] Clicked Turnstile CAPTCHA")
                except Exception:
                    pass

                _time.sleep(5)

                # Trigger XSRF cookie by making an API call from the browser
                try:
                    page.evaluate("""
                        fetch('/api/authentication/claims', {
                            credentials: 'include'
                        }).then(r => r.json()).catch(() => {})
                    """)
                    _time.sleep(2)
                except Exception:
                    pass

                # Extract cookies via JS (Camoufox uses Firefox, not Selenium)
                cookies_list = page.context.cookies()
                cookies = {c["name"]: c["value"] for c in cookies_list}
                xsrf = cookies.get("XSRF-TOKEN", "")

                # Fallback: try other common XSRF cookie names
                if not xsrf:
                    for name in cookies:
                        nl = name.lower()
                        if "xsrf" in nl or "csrf" in nl or "antiforgery" in nl:
                            xsrf = cookies[name]
                            logger.info("[ELDO] Found XSRF-like cookie: %s", name)
                            break

                # Check if logged in
                url = page.url
                logged_in = "login" not in url.lower()

                self.data = {
                    "cookies": cookies,
                    "xsrf_token": xsrf,
                    "user_agent": DEFAULT_USER_AGENT,
                    "logged_in": logged_in,
                }
                self.captured_at = time.time()
                logger.info("[ELDO] Camoufox capture: cookies=%d | xsrf=%s | logged_in=%s | url=%s",
                             len(cookies), "yes" if xsrf else "no", logged_in, url[:60])

                # Log all cookie names for debugging (only if no xsrf)
                if not xsrf:
                    token_names = [n for n in cookies if any(
                        x in n.lower() for x in ['token', 'xsrf', 'csrf', 'session', 'auth']
                    )]
                    logger.info("[ELDO] Token-like cookies: %s", token_names or "none found")

                if not logged_in:
                    logger.warning("[ELDO] Not logged in. Run login tool first.")
                return self.data

        except Exception as e:
            logger.error("[ELDO] Camoufox capture failed: %s", e)
            return {}

    def close(self):
        # Camoufox manages its own lifecycle
        pass


# ── Global state ──

g2g_auth = G2GAuth()
eldo_auth = EldoAuth()
db: Database = None
_shutdown = asyncio.Event()
started_at = time.time()


# ── HTTP Handlers ──

async def handle_auth_g2g(request: web.Request):
    if not g2g_auth.is_fresh():
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, g2g_auth.capture)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        if not data:
            return web.json_response({"error": "JWT capture failed"}, status=503)
    return web.json_response(g2g_auth.data)


async def handle_auth_eldo(request: web.Request):
    if not eldo_auth.is_fresh():
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, eldo_auth.capture)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        if not data:
            return web.json_response({"error": "Auth capture failed"}, status=503)
    return web.json_response(eldo_auth.data)


async def handle_health(request: web.Request):
    g2g_jwt_exp = 0
    if g2g_auth.data and g2g_auth.data.get("jwt_token"):
        exp = _jwt_exp(g2g_auth.data["jwt_token"])
        if exp:
            g2g_jwt_exp = max(0, exp - time.time())

    return web.json_response({
        "status": "ok",
        "uptime": int(time.time() - started_at),
        "g2g": {
            "has_jwt": bool(g2g_auth.data and g2g_auth.data.get("jwt_token")),
            "jwt_expires_in": int(g2g_jwt_exp),
            "fresh": g2g_auth.is_fresh(),
            "active_profile": g2g_auth.profile_dir,
            "cookies": len(g2g_auth.data.get("cookies", {})) if g2g_auth.data else 0,
        },
        "eldo": {
            "has_cookies": bool(eldo_auth.data and eldo_auth.data.get("cookies")),
            "fresh": eldo_auth.is_fresh(),
            "active_profile": eldo_auth.profile_dir,
            "cookies": len(eldo_auth.data.get("cookies", {})) if eldo_auth.data else 0,
            "xsrf": bool(eldo_auth.data and eldo_auth.data.get("xsrf_token")),
            "logged_in": bool(eldo_auth.data and eldo_auth.data.get("logged_in")),
        },
    })


async def handle_refresh_eldo(request: web.Request):
    eldo_auth.data = None
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, eldo_auth.capture)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=503)
    if not data:
        return web.json_response({"error": "Auth capture failed"}, status=503)
    return web.json_response(eldo_auth.data)


async def handle_login_status(request: web.Request):
    return web.json_response(login_state.to_dict())


async def handle_otp(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    code = body.get("otp", "")
    if not code:
        return web.json_response({"error": "OTP code required"}, status=400)
    login_state.submit_otp(code)
    logger.info("[G2G] OTP received from dashboard: %s***", code[:2])
    return web.json_response({"status": "ok"})


async def handle_profile_status(request: web.Request):
    """Return profile status. Active profiles always live from auth objects,
    backup profiles from periodic cache."""
    result = dict(profile_status)

    # Always inject live status for active profiles
    g2g_alive = bool(g2g_auth.data and g2g_auth.data.get("jwt_token"))
    result.setdefault(g2g_auth.profile_dir, {})["platform"] = "g2g"
    result[g2g_auth.profile_dir]["is_active"] = True
    result[g2g_auth.profile_dir]["alive"] = g2g_alive
    result[g2g_auth.profile_dir]["detail"] = "JWT OK" if g2g_alive else "no JWT"
    result[g2g_auth.profile_dir]["checked_at"] = time.time()

    eldo_alive = bool(eldo_auth.data and eldo_auth.data.get("logged_in"))
    result.setdefault(eldo_auth.profile_dir, {})["platform"] = "eldo"
    result[eldo_auth.profile_dir]["is_active"] = True
    result[eldo_auth.profile_dir]["alive"] = eldo_alive
    result[eldo_auth.profile_dir]["detail"] = "logged in" if eldo_alive else "not logged in"
    result[eldo_auth.profile_dir]["checked_at"] = time.time()

    return web.json_response({"profiles": result})


async def handle_relogin_profile(request: web.Request):
    """Force re-login on G2G. Triggers auto-login flow."""
    profile = request.match_info.get("profile", "")
    if profile not in G2G_PROFILES:
        return web.json_response({"error": f"Unknown profile: {profile}"}, status=400)

    loop = asyncio.get_running_loop()

    def _do_relogin():
        g2g_auth._captcha_until = 0  # Clear captcha cooldown
        g2g_auth.data = None
        g2g_auth._consecutive_failures = 0
        return g2g_auth.capture()

    try:
        data = await loop.run_in_executor(None, _do_relogin)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=503)
    if not data:
        return web.json_response({"error": "Relogin failed", "login_status": login_state.to_dict()}, status=503)
    return web.json_response({"status": "ok", "profile": profile, "login_status": login_state.to_dict()})


async def handle_logs(request: web.Request):
    """Return recent auth log lines."""
    n = int(request.query.get("n", "100"))
    try:
        with open("/tmp/auth6.log", "r", errors="replace") as f:
            lines = f.readlines()[-n:]
        return web.json_response({"lines": [l.rstrip() for l in lines]})
    except Exception as e:
        return web.json_response({"lines": [], "error": str(e)})


# ── Main ──

async def run_auth_service():
    global db
    db = Database(DATABASE_PATH)

    # Clear stale login state from previous run
    login_state.reset()

    app = web.Application()
    app.router.add_get("/auth/g2g", handle_auth_g2g)
    app.router.add_get("/auth/eldo", handle_auth_eldo)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/auth/eldo/refresh", handle_refresh_eldo)
    app.router.add_get("/auth/login-status", handle_login_status)
    app.router.add_post("/auth/otp", handle_otp)
    app.router.add_get("/auth/profile-status", handle_profile_status)
    app.router.add_post("/auth/relogin/{profile}", handle_relogin_profile)
    app.router.add_get("/auth/logs", handle_logs)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", AUTH_PORT)
    await site.start()
    logger.info("Auth service listening on port %d (PID: %d)", AUTH_PORT, os.getpid())

    # Heartbeat — start BEFORE initial capture so watchdog doesn't restart us
    async def heartbeat():
        while not _shutdown.is_set():
            db.update_heartbeat("auth_service", os.getpid())
            await asyncio.sleep(30)
    heartbeat_task = asyncio.create_task(heartbeat())

    # Initial capture
    loop = asyncio.get_running_loop()
    logger.info("Capturing initial auth...")
    try:
        await loop.run_in_executor(None, g2g_auth.capture)
    except Exception as e:
        logger.error("Initial G2G auth failed: %s", e)
    try:
        await loop.run_in_executor(None, eldo_auth.capture)
    except Exception as e:
        logger.error("Initial Eldo auth failed: %s", e)

    # Profile health check — test all profiles every 5 min
    async def profile_check_loop():
        while not _shutdown.is_set():
            await asyncio.sleep(300)  # 5 min
            if _shutdown.is_set():
                break
            logger.info("Profile health check...")

            # ── G2G — single profile, live status from auth object ──
            g2g_alive = bool(g2g_auth.data and g2g_auth.data.get("jwt_token"))
            profile_status[G2G_PROFILES[0]] = {
                "platform": "g2g",
                "alive": g2g_alive,
                "detail": "JWT OK" if g2g_alive else "no JWT",
                "checked_at": time.time(),
                "is_active": True,
            }
            logger.info("[G2G] Profile: %s", "JWT OK" if g2g_alive else "no JWT")

            # ── Eldorado profiles ──
            for profile in ELDO_PROFILES:
                is_active = (profile == eldo_auth.profile_dir)
                path = Path.cwd() / profile
                exists = path.exists()
                detail = "exists" if exists else "missing"
                profile_status[profile] = {
                    "platform": "eldo",
                    "alive": exists,
                    "detail": detail,
                    "checked_at": time.time(),
                    "is_active": is_active,
                }
                logger.info("[ELDO] Profile %s: %s (active=%s)", profile, detail, is_active)

    asyncio.create_task(profile_check_loop())

    await _shutdown.wait()

    logger.info("Shutting down...")
    await runner.cleanup()
    g2g_auth.close()
    eldo_auth.close()
    logger.info("Auth service stopped")


def main():
    def handle_signal(sig, frame):
        _shutdown.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(run_auth_service())
    except KeyboardInterrupt:
        _shutdown.set()


if __name__ == "__main__":
    main()
