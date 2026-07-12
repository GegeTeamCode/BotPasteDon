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
from typing import Optional

from aiohttp import web
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from shared.alerts import clear_ops_alert, send_ops_alert
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
# Protected sell page — triggers authenticated API XHRs from page JS which we
# scrape via CDP for the anti-fraud / build-time headers Eldorado expects. URL
# discovered from the G2G-AutomationBot-v4 reference; /seller/orders returns
# SSR 404 and never bootstraps the auth client.
ELDO_SELL_PAGE = "https://www.eldorado.gg/dashboard/orders/sold"
# Cheap authenticated probe to verify cookies are actually accepted by the API.
ELDO_API_PROBE = "https://www.eldorado.gg/api/orders/me/statesCount"

# Eldorado's own backend refresh endpoint. POSTing to it with the cached
# RefreshToken cookie (and the XSRF + build-time headers) returns Set-Cookie
# headers carrying a fresh IdToken (and rotated RefreshToken). Discovered via
# CDP probe — direct AWS Cognito calls fail with SECRET_HASH errors because
# Eldorado's Cognito client is configured with a client secret we don't have.
ELDO_REFRESH_URL = "https://www.eldorado.gg/api/authentication/refreshTokens"

G2G_PROFILES = ["chrome_profile_g2g"]
ELDO_PROFILES = ["chrome_profile_eldo", "chrome_profile_eldo_bak1", "chrome_profile_eldo_bak2"]

JWT_TTL = 780  # 13 min — refresh before JWT expires (15 min)


_LOCK_FILES = (
    "parent.lock", ".parentlock", "lock",
    "SingletonLock", "SingletonCookie", "SingletonSocket",
)


def _cleanup_profile_locks(profile_dir: str) -> None:
    """Remove stale browser lock files. Browsers killed uncleanly leave these
    behind, blocking subsequent launches with "already running" errors."""
    p = Path.cwd() / profile_dir
    if not p.exists():
        return
    for fname in _LOCK_FILES:
        try:
            (p / fname).unlink(missing_ok=True)
        except Exception:
            pass


def _kill_orphan_browsers() -> None:
    """Kill orphan browser/driver processes tied to bot profiles. Run on
    startup + shutdown so leftovers don't hold profile singleton locks."""
    import subprocess
    patterns = ["camoufox-bin", "chromedriver"] + G2G_PROFILES + ELDO_PROFILES
    for pat in patterns:
        try:
            subprocess.run(
                ["pkill", "-9", "-f", pat],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass


def _jwt_exp(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _jwt_claim(token: str, claim: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get(claim)
    except Exception:
        return None


def _find_local_chromedriver():
    """Locate chromedriver binary in ~/.wdm without invoking webdriver_manager.

    Avoids the wdm FileLock that can leak FDs into the long-running auth
    process and self-deadlock subsequent driver creations.
    """
    import glob
    pattern = os.path.expanduser(
        "~/.wdm/drivers/chromedriver/linux64/*/chromedriver-linux64/chromedriver"
    )
    candidates = sorted(glob.glob(pattern), reverse=True)
    return candidates[0] if candidates else None


def _create_driver(profile_dir: str):
    """Create Chrome driver with performance logging for CDP capture."""
    _cleanup_profile_locks(profile_dir)
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

    # Prefer locally-installed chromedriver to avoid webdriver_manager filelock
    # leak. Fallback to ChromeDriverManager only on first install.
    local = _find_local_chromedriver()
    if local:
        service = Service(local)
    else:
        # Defensive: clear any stale wdm lock before falling back to install
        try:
            os.remove(os.path.expanduser("~/.wdm/.wdm-lock-chromedriver-linux64"))
        except FileNotFoundError:
            pass
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
        self.refresh_token_exp_ms = 0  # epoch ms; when refresh_token dies → re-login needed
        self.refresh_last_ok = 0       # last successful backend refresh (chain-health signal)

    def _try_backend_refresh(self) -> Optional[dict]:
        """Mint a fresh JWT via the G2G refresh endpoint — no browser. Only
        works after a prior successful capture (we need refresh_token,
        active_device_token, long_lived_token cookies + current JWT for sub)."""
        if not self.data:
            return None
        cur_jwt = self.data.get("jwt_token") or ""
        cookies = self.data.get("cookies") or {}
        if not cur_jwt or not cookies.get("refresh_token"):
            return None
        result = _g2g_backend_refresh(cur_jwt, cookies,
                                       self.data.get("user_agent", DEFAULT_USER_AGENT))
        if not result:
            return None
        new_jwt = result["jwt_token"]
        new_cookies = result["cookies"]
        # Validate the new JWT is decodable and not expired.
        exp = _jwt_exp(new_jwt)
        if not exp or exp <= time.time():
            logger.warning("[G2G] backend refresh returned JWT but exp invalid/past")
            return None
        self.data = {
            "jwt_token": new_jwt,
            "cookies": new_cookies,
            "user_agent": self.data.get("user_agent", DEFAULT_USER_AGENT),
        }
        self.captured_at = time.time()
        self.refresh_last_ok = time.time()
        if result.get("refresh_token_exp"):
            self.refresh_token_exp_ms = result["refresh_token_exp"]
        self._consecutive_failures = 0
        logger.info(
            "[G2G] backend refresh OK | new JWT exp=%dmin | %d cookies",
            int((exp - time.time()) / 60), len(new_cookies),
        )
        return self.data

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
        # Fast path: try backend refresh first if we already have a JWT bundle.
        # ~1s HTTP call vs ~30-60s Selenium full capture. Falls through to the
        # browser path only on first capture or when refresh_token has expired.
        if self.data and self.data.get("jwt_token") and self.data.get("cookies", {}).get("refresh_token"):
            logger.info("[G2G] Trying backend refresh (no browser)")
            refreshed = self._try_backend_refresh()
            if refreshed:
                return refreshed
            logger.info("[G2G] Backend refresh failed — falling back to browser")

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


def _eldo_api_probe(cookies: dict, xsrf: str, user_agent: str = "",
                    nsure_device_id: str = "", x_client_build_time: str = "") -> bool:
    """Probe ELDO_API_PROBE with the given auth bundle. Returns True iff the
    response is 200 — the only way to be sure the cookies actually authenticate."""
    if not cookies or not xsrf:
        return False
    from curl_cffi import requests as _cffi
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "user-agent": user_agent or DEFAULT_USER_AGENT,
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.eldorado.gg",
        "referer": ELDO_SELL_PAGE,
        "x-xsrf-token": xsrf,
        "cookie": cookie_header,
    }
    if nsure_device_id:
        headers["nsure-device-id"] = nsure_device_id
    if x_client_build_time:
        headers["x-client-build-time"] = x_client_build_time
    try:
        r = _cffi.get(ELDO_API_PROBE, headers=headers, timeout=10, impersonate="chrome120")
        return r.status_code == 200
    except Exception:
        return False


G2G_REFRESH_URL = "https://sls.g2g.com/user/refresh_access"


def _g2g_backend_refresh(jwt: str, cookies: dict, user_agent: str = "") -> Optional[dict]:
    """POST to G2G's own JWT refresh endpoint. Body schema (discovered from
    /js/app.*.js bundle):
      {user_id, refresh_token, active_device_token, long_lived_token}
    Response 200 payload:
      {access_token, access_token_exp, refresh_token, refresh_token_exp,
       active_device_token, active_device_token_exp,
       long_lived_token, long_lived_token_exp}
    All three *_token cookies are returned with rolling expiries, so as long
    as we refresh inside the refresh_token window (~12 days, slides every
    call) we can mint forever without re-opening Chrome.

    Returns {"jwt_token": <new>, "cookies": {<updated cookies>}} on success,
    else None.
    """
    if not jwt:
        logger.debug("[G2G] backend refresh skipped — no current JWT")
        return None
    user_id = _jwt_claim(jwt, "sub")
    if not user_id:
        logger.debug("[G2G] backend refresh skipped — JWT missing sub")
        return None
    rt = cookies.get("refresh_token")
    if not rt:
        logger.debug("[G2G] backend refresh skipped — no refresh_token cookie")
        return None

    from curl_cffi import requests as _cffi
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "user-agent": user_agent or DEFAULT_USER_AGENT,
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.g2g.com",
        "referer": "https://www.g2g.com/",
        "content-type": "application/json",
        "authorization": f"Bearer {jwt}",
        "cookie": cookie_header,
    }
    body = {
        "user_id": user_id,
        "refresh_token": rt,
        "active_device_token": cookies.get("active_device_token", ""),
        "long_lived_token": cookies.get("long_lived_token", ""),
    }
    try:
        r = _cffi.post(G2G_REFRESH_URL, headers=headers, json=body,
                       timeout=15, impersonate="chrome120")
    except Exception as e:
        logger.warning("[G2G] backend refresh exception: %s", e)
        return None
    if r.status_code != 200:
        body_preview = (r.text or "")[:200]
        logger.warning("[G2G] backend refresh HTTP %d | body=%s",
                       r.status_code, body_preview)
        return None
    try:
        j = r.json()
    except Exception as e:
        logger.warning("[G2G] backend refresh 200 but not JSON: %s", e)
        return None
    if j.get("code") != 2000:
        logger.warning("[G2G] backend refresh code=%s messages=%s",
                       j.get("code"), j.get("messages"))
        return None
    payload = j.get("payload") or {}
    new_jwt = payload.get("access_token") or ""
    if not new_jwt:
        logger.warning("[G2G] backend refresh OK but payload missing access_token")
        return None
    new_cookies = dict(cookies)
    for ck in ("refresh_token", "active_device_token", "long_lived_token"):
        v = payload.get(ck)
        if v:
            new_cookies[ck] = v
    return {
        "jwt_token": new_jwt,
        "cookies": new_cookies,
        # epoch ms — when the refresh_token itself expires (slides ~12 days on
        # every refresh). Surfaced on the dashboard as the "re-login by" countdown.
        "refresh_token_exp": payload.get("refresh_token_exp"),
    }


def _eldo_backend_refresh(cookies: dict, xsrf_token: str, user_agent: str = "",
                          x_client_build_time: str = "") -> Optional[dict]:
    """POST to Eldorado's own refresh endpoint with the cached cookies. The
    server reads `__Host-EldoradoRefreshToken` from the Cookie header and
    responds with `Set-Cookie` headers carrying a fresh IdToken (and often a
    rotated RefreshToken too).

    Returns a {cookie_name: value} dict of UPDATED cookies on 200, else None.
    Headers were captured from a real Firefox session via Playwright probe.
    """
    if not cookies.get("__Host-EldoradoRefreshToken"):
        logger.debug("[ELDO] backend refresh skipped — no RefreshToken cookie")
        return None
    if not xsrf_token:
        logger.debug("[ELDO] backend refresh skipped — no XSRF token")
        return None

    from curl_cffi import requests as _cffi
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.5",
        "content-type": "application/json",
        "origin": "https://www.eldorado.gg",
        "referer": ELDO_SELL_PAGE,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": user_agent or DEFAULT_USER_AGENT,
        "x-xsrf-token": xsrf_token,
        "cookie": cookie_header,
    }
    if x_client_build_time:
        headers["x-client-build-time"] = x_client_build_time

    try:
        r = _cffi.post(
            ELDO_REFRESH_URL,
            headers=headers,
            data="{}",
            impersonate="chrome136",
            timeout=20,
        )
    except Exception as e:
        logger.warning("[ELDO] backend refresh exception: %s", e)
        return None

    if r.status_code != 200:
        body = (r.text or "")[:300]
        logger.warning("[ELDO] backend refresh HTTP %d | body=%s", r.status_code, body)
        return None

    # Pull Set-Cookie values out of the response. curl_cffi returns headers as
    # a multi-value dict; we want the new __Host-Eldorado* cookies.
    updated = {}
    try:
        # curl_cffi response.cookies is a SimpleCookie-like dict
        for name, val in r.cookies.items():
            updated[name] = val
    except Exception:
        pass
    # Fallback: parse Set-Cookie header(s) manually
    if not updated:
        try:
            set_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []
            if not set_cookies:
                raw = r.headers.get("set-cookie", "")
                if raw:
                    set_cookies = [raw]
            for line in set_cookies:
                name, _, rest = line.partition("=")
                val, _, _ = rest.partition(";")
                if name:
                    updated[name.strip()] = val.strip()
        except Exception:
            pass

    if not updated.get("__Host-EldoradoIdToken"):
        logger.warning("[ELDO] backend refresh 200 but no new IdToken in Set-Cookie | body=%s",
                       (r.text or "")[:200])
        return None
    logger.info("[ELDO] backend refresh OK | %d cookies updated (idToken refreshed)", len(updated))
    return updated


def _eldo_capture_isolated(profile_dir: str, timeout_sec: int = 200) -> dict:
    """Run one Camoufox capture in a spawned subprocess with a hard timeout.

    Why a subprocess: the Playwright node driver occasionally crashes mid-capture
    (coreBundle.js TypeError on a page error with no `location`); Camoufox.__exit__
    then calls browser.close() on the dead connection and Playwright's sync
    `_sync()` busy-loops forever holding the GIL — a 100%-CPU thread that cannot
    be killed in-process (this was the recurring "python kẹt 100%" incident). In a
    child process the hang is contained: on timeout we SIGKILL the process group
    and move on to the next profile. `spawn` (not fork) avoids forking this
    multi-threaded asyncio service.
    """
    import multiprocessing as _mp
    import queue as _queue
    # Worker lives in its own module (auth._capture_proc) so multiprocessing
    # spawn re-imports it cleanly regardless of how this service was launched
    # (`python -m auth.main` makes THIS module __main__, which complicates spawn).
    from auth._capture_proc import capture_worker

    ctx = _mp.get_context("spawn")
    result_q = ctx.Queue()
    proc = ctx.Process(target=capture_worker, args=(profile_dir, result_q))
    proc.start()
    data = {}
    try:
        data = result_q.get(timeout=timeout_sec)
    except _queue.Empty:
        logger.error("[ELDO] capture timed out on %s (%ds) — killing browser "
                     "subprocess tree (Playwright close() hang)", profile_dir, timeout_sec)
    except Exception as e:
        logger.error("[ELDO] capture subprocess error on %s: %s", profile_dir, e)
    finally:
        # Kill the worker's whole process group (node + camoufox-bin). Targeting
        # the child's own pid only ever matches its setsid group, never ours.
        if proc.pid:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.join(timeout=10)
        if proc.is_alive():
            try:
                proc.kill()
            except Exception:
                pass
            proc.join(timeout=5)
        try:
            result_q.close()
        except Exception:
            pass
    return data or {}


def _read_eldo_disk_cookies(profile_dir: str) -> dict:
    """Read eldorado.gg cookies (name -> value) from a profile's cookies.sqlite.

    Lets a cold-start backend refresh reuse a still-valid on-disk RefreshToken
    instead of opening Camoufox — the browser path STRIPS the on-disk session
    (Eldo set-cookie clears __Host-Eldorado* when the IdToken has expired),
    permanently logging the profile out.
    """
    import sqlite3
    p = Path.cwd() / profile_dir / "cookies.sqlite"
    if not p.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT name, value, host FROM moz_cookies").fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[ELDO] read disk cookies failed for %s: %s", profile_dir, e)
        return {}
    return {n: v for n, v, h in rows if "eldorado" in (h or "")}


def _eldo_disk_refresh_expiry(profile_dir: str):
    """Return the on-disk __Host-EldoradoRefreshToken expiry (epoch seconds),
    or None when the profile has no RefreshToken left (logged out / never
    logged in). Used by the profile health check to warn BEFORE a profile is
    needed for failover and turns out to be dead."""
    import sqlite3
    p = Path.cwd() / profile_dir / "cookies.sqlite"
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT expiry FROM moz_cookies "
                "WHERE name = '__Host-EldoradoRefreshToken' AND host LIKE '%eldorado%' "
                "ORDER BY expiry DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[ELDO] read disk RefreshToken expiry failed for %s: %s", profile_dir, e)
        return None
    return row[0] if row else None


class EldoAuth(PlatformAuth):
    """Eldorado auth using Camoufox (bypass Cloudflare Turnstile)."""

    def __init__(self):
        super().__init__(ELDO_PROFILES[0], "eldo")
        self._profile_idx = 0
        self._consecutive_failures = 0
        self._last_failure_time = 0
        # Captured per-cycle headers (from CDP). Eldo's API requires these.
        self._nsure_device_id: str = ""
        self._x_client_build_time: str = ""
        # Last-known token-bearing cookies, kept across captures so a Cognito-
        # only refresh can rebuild a complete auth bundle without re-running
        # Camoufox.
        self._last_cookies: dict = {}
        self._last_user_agent: str = DEFAULT_USER_AGENT

    def _try_backend_refresh(self) -> Optional[dict]:
        """Mint a fresh IdToken via Eldorado's /api/authentication/refreshTokens
        endpoint without a browser. Returns a full auth `data` dict, or None to
        fall back to the Camoufox path.

        Tries the warm in-memory bundle first, then — crucially on cold start —
        reads a still-valid RefreshToken off each profile's cookies.sqlite. This
        keeps us off the Camoufox path, which STRIPS the on-disk session (Eldo
        set-cookie clears __Host-Eldorado* when the IdToken has expired) and
        permanently logs the profile out.
        """
        # Warm path: bundle kept in memory from a previous refresh/capture.
        data = self._backend_refresh_with(self._last_cookies, self._last_user_agent)
        if data:
            return data

        # Cold path: reuse a valid RefreshToken straight off disk, per profile.
        for i in range(len(ELDO_PROFILES)):
            profile = ELDO_PROFILES[(self._profile_idx + i) % len(ELDO_PROFILES)]
            disk = _read_eldo_disk_cookies(profile)
            if not disk.get("__Host-EldoradoRefreshToken"):
                continue
            logger.info("[ELDO] Backend refresh from on-disk cookies of %s", profile)
            data = self._backend_refresh_with(disk, self._last_user_agent or DEFAULT_USER_AGENT)
            if data:
                self.profile_dir = profile
                self._profile_idx = ELDO_PROFILES.index(profile)
                return data
        return None

    def _backend_refresh_with(self, base_cookies: dict, user_agent: str) -> Optional[dict]:
        """Run one backend refresh against `base_cookies`. Returns data or None."""
        rt = (base_cookies or {}).get("__Host-EldoradoRefreshToken")
        xsrf = (base_cookies or {}).get("__Host-XSRF-TOKEN", "")
        if not rt or not xsrf:
            return None

        logger.info("[ELDO] Trying backend refresh (no browser)")
        updated = _eldo_backend_refresh(
            base_cookies, xsrf, user_agent,
            x_client_build_time=self._x_client_build_time,
        )
        if not updated:
            logger.info("[ELDO] Backend refresh failed — fallback to Camoufox")
            return None

        # Merge Set-Cookie updates into the bundle.
        cookies = dict(base_cookies)
        cookies.update(updated)
        # Refresh xsrf if the server rotated it.
        new_xsrf = updated.get("__Host-XSRF-TOKEN") or xsrf

        # Probe API to confirm the new IdToken actually authenticates.
        api_verified = _eldo_api_probe(
            cookies, new_xsrf, user_agent,
            nsure_device_id=self._nsure_device_id,
            x_client_build_time=self._x_client_build_time,
        )
        if not api_verified:
            logger.warning("[ELDO] Backend refresh returned tokens but API probe still failed")
            return None

        logger.info("[ELDO] Backend refresh OK (api_ok=True, %d cookies)", len(cookies))
        return {
            "cookies": cookies,
            "xsrf_token": new_xsrf,
            "user_agent": user_agent,
            "logged_in": True,
            "api_verified": True,
            "nsure_device_id": self._nsure_device_id,
            "x_client_build_time": self._x_client_build_time,
            "refreshed_via": "eldo_backend",
        }

    def _next_profile(self):
        self._profile_idx = (self._profile_idx + 1) % len(ELDO_PROFILES)
        self.profile_dir = ELDO_PROFILES[self._profile_idx]
        logger.info("[ELDO] Switching to profile: %s", self.profile_dir)

    @staticmethod
    def _capture_single(profile_dir: str) -> dict:
        """Capture auth from a single profile. Returns data or {}.

        Static + module-reachable so it can run inside an isolated subprocess
        (see _eldo_capture_isolated). Always invoke it through that wrapper, never
        directly — the Playwright node driver can crash mid-capture (coreBundle.js
        bug) and the sync close() in Camoufox.__exit__ then busy-loops forever
        holding the GIL; only killing a separate process recovers from that.
        """
        import time as _time
        from pathlib import Path

        # Isolate from main thread's asyncio event loop — Playwright sync API
        # refuses to run if it detects a running event loop, and on Python <3.12
        # asyncio.get_event_loop() leaks the main thread's loop into worker threads.
        asyncio.set_event_loop(asyncio.new_event_loop())

        profile_path = Path.cwd() / profile_dir
        profile_path.mkdir(parents=True, exist_ok=True)
        _cleanup_profile_locks(profile_dir)

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

                # CDP listener: Eldorado's API requires `nsure-device-id` (anti-
                # fraud) and `x-client-build-time` (build stamp). These headers
                # are set by page JS at request time and never appear as cookies,
                # so we have to scrape them off in-flight via CDP.
                cdp_captured = {}
                try:
                    cdp = page.context.new_cdp_session(page)
                    cdp.send("Network.enable")

                    def _on_cdp_request(params):
                        try:
                            req = params.get("request", {})
                            url = req.get("url", "")
                            h = {str(k).lower(): str(v)
                                 for k, v in (req.get("headers") or {}).items()}
                            if h.get("nsure-device-id") and not cdp_captured.get("nsure_device_id"):
                                cdp_captured["nsure_device_id"] = h["nsure-device-id"]
                            if "eldorado.gg/api/" in url:
                                for key in ("x-xsrf-token", "x-client-build-time", "user-agent"):
                                    if h.get(key) and not cdp_captured.get(key):
                                        cdp_captured[key] = h[key]
                        except Exception:
                            pass

                    cdp.on("Network.requestWillBeSent", _on_cdp_request)
                except Exception as _cdp_err:
                    logger.debug("[ELDO] CDP listener unavailable: %s", _cdp_err)

                # Phase 1: homepage — let Cloudflare resolve any challenge before
                # we hit a protected route.
                page.goto(ELDO_HOME, timeout=60000, wait_until="domcontentloaded")
                for _ in range(15):
                    try:
                        title = (page.title() or "").lower()
                    except Exception:
                        title = ""
                    if "just a moment" not in title and "cloudflare" not in title:
                        break
                    _time.sleep(2)
                _time.sleep(3)

                # Handle Cloudflare Turnstile (rare, only on first capture per profile).
                try:
                    turnstile = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                    if turnstile:
                        turnstile.locator("body").click()
                        _time.sleep(3)
                        logger.info("[ELDO] Clicked Turnstile CAPTCHA")
                except Exception:
                    pass

                # Phase 2: sell page — triggers the authenticated XHR pattern
                # used by the API (gets us nsure-device-id + x-client-build-time
                # via CDP). Also forces a Cognito access-token refresh on the
                # page side when the cookie is near expiry.
                page.goto(ELDO_SELL_PAGE, timeout=60000, wait_until="domcontentloaded")
                _time.sleep(5)
                try:
                    page.evaluate("window.scrollBy(0, 500)")
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                _time.sleep(2)

                # Phase 3: explicit XSRF kick (defensive — usually already set
                # by the sell page above).
                try:
                    page.evaluate("""
                        fetch('/api/authentication/claims', {credentials: 'include'})
                        .then(r => r.json()).catch(() => {})
                    """)
                    _time.sleep(2)
                except Exception:
                    pass

                # Extract cookies (all eldorado.gg + login.eldorado.gg)
                cookies_list = page.context.cookies()
                cookies = {c["name"]: c["value"] for c in cookies_list}

                # Pull headers captured by CDP, with sensible fallbacks.
                nsure_device_id = cdp_captured.get("nsure_device_id", "")
                x_client_build_time = cdp_captured.get("x-client-build-time", "")
                user_agent = cdp_captured.get("user-agent") or DEFAULT_USER_AGENT
                xsrf = cdp_captured.get("x-xsrf-token") or cookies.get("__Host-XSRF-TOKEN", "")
                if not xsrf:
                    for name in cookies:
                        nl = name.lower()
                        if "xsrf" in nl or "csrf" in nl or "antiforgery" in nl:
                            xsrf = cookies[name]
                            logger.info("[ELDO] Fallback XSRF from cookie: %s", name)
                            break

                url = page.url
                url_logged_in = "login" not in url.lower()
                has_id_token = bool(cookies.get("__Host-EldoradoIdToken"))
                has_refresh_token = bool(cookies.get("__Host-EldoradoRefreshToken"))

                # Probe the real API with the full header set Eldo expects.
                api_verified = _eldo_api_probe(
                    cookies, xsrf, user_agent,
                    nsure_device_id=nsure_device_id,
                    x_client_build_time=x_client_build_time,
                )
                if not api_verified:
                    logger.warning("[ELDO] API probe failed on %s (idToken=%s refreshToken=%s)",
                                   profile_dir, has_id_token, has_refresh_token)

                logged_in = url_logged_in and api_verified

                data = {
                    "cookies": cookies,
                    "xsrf_token": xsrf,
                    "user_agent": user_agent,
                    "logged_in": logged_in,
                    "api_verified": api_verified,
                    "nsure_device_id": nsure_device_id,
                    "x_client_build_time": x_client_build_time,
                    "refreshed_via": "camoufox",
                }
                logger.info(
                    "[ELDO] Capture: cookies=%d | xsrf=%s | nsure=%s | build=%s | idToken=%s | "
                    "refresh=%s | url_ok=%s | api_ok=%s | url=%s | profile=%s",
                    len(cookies), "yes" if xsrf else "no",
                    "yes" if nsure_device_id else "no",
                    "yes" if x_client_build_time else "no",
                    "yes" if has_id_token else "no",
                    "yes" if has_refresh_token else "no",
                    url_logged_in, api_verified, url[:60], profile_dir,
                )

                if not logged_in:
                    logger.warning("[ELDO] Not logged in on profile: %s (url_ok=%s api_ok=%s)",
                                   profile_dir, url_logged_in, api_verified)
                return data

        except Exception as e:
            logger.error("[ELDO] Camoufox capture failed on %s: %s", profile_dir, e)
            return {}

    def _remember_for_refresh(self, data: dict) -> None:
        """Cache the bits needed for a Cognito-only refresh next cycle."""
        if not data:
            return
        cookies = data.get("cookies") or {}
        if cookies:
            self._last_cookies = dict(cookies)
        ua = data.get("user_agent")
        if ua:
            self._last_user_agent = ua
        nd = data.get("nsure_device_id")
        if nd:
            self._nsure_device_id = nd
        bt = data.get("x_client_build_time")
        if bt:
            self._x_client_build_time = bt

    def capture(self) -> dict:
        # Back off if capture keeps failing
        if self._consecutive_failures >= 3:
            since_last = time.time() - self._last_failure_time
            if since_last < 300:
                logger.warning("[ELDO] Skipping capture — cooling down (%d failures, %.0fs ago)",
                               self._consecutive_failures, since_last)
                return self.data or {}

        # Fast path: Cognito-only refresh using the RefreshToken cookie we
        # stashed last cycle. Avoids spinning Camoufox most of the time.
        backend_data = self._try_backend_refresh()
        if backend_data:
            self.data = backend_data
            self.captured_at = time.time()
            self._remember_for_refresh(backend_data)
            self._consecutive_failures = 0
            clear_ops_alert("eldo-all")
            clear_ops_alert(f"eldo-profile:{self.profile_dir}")
            return backend_data

        # Snapshot the current Cognito tokens so we can detect when a Camoufox
        # capture has stripped them (Eldo's response set-cookie on a failed
        # session probe clears RefreshToken/IdToken — we don't want to overwrite
        # a still-valid bundle with that stripped version).
        prev_had_refresh = bool((self._last_cookies or {}).get("__Host-EldoradoRefreshToken"))
        prev_had_id = bool((self._last_cookies or {}).get("__Host-EldoradoIdToken"))

        for i in range(len(ELDO_PROFILES)):
            profile = ELDO_PROFILES[(self._profile_idx + i) % len(ELDO_PROFILES)]
            # Isolate each attempt in its own subprocess: keeps Playwright's
            # asyncio/sync state out of this service's threads AND lets a hung
            # close() (node driver crash) be SIGKILLed instead of spinning the GIL.
            data = _eldo_capture_isolated(profile)
            if data and data.get("logged_in") and data.get("xsrf_token"):
                # Guard: if we used to have RefreshToken and now we don't, the
                # capture stripped our credentials — Eldo's response wiped the
                # auth cookies. Discard the bad result, keep the previous data.
                new_cookies = data.get("cookies") or {}
                stripped = (
                    prev_had_refresh and not new_cookies.get("__Host-EldoradoRefreshToken")
                ) or (
                    prev_had_id and not new_cookies.get("__Host-EldoradoIdToken")
                )
                if stripped:
                    logger.warning(
                        "[ELDO] Capture stripped auth cookies on %s — discarding result, "
                        "keeping previous bundle (prev_had_refresh=%s prev_had_id=%s "
                        "new_has_refresh=%s new_has_id=%s)",
                        profile, prev_had_refresh, prev_had_id,
                        bool(new_cookies.get("__Host-EldoradoRefreshToken")),
                        bool(new_cookies.get("__Host-EldoradoIdToken")),
                    )
                    self._next_profile()
                    continue
                self.data = data
                self.captured_at = time.time()
                self.profile_dir = profile
                self._profile_idx = ELDO_PROFILES.index(profile)
                self._consecutive_failures = 0
                self._remember_for_refresh(data)
                clear_ops_alert("eldo-all")
                clear_ops_alert(f"eldo-profile:{profile}")
                return data
            logger.warning("[ELDO] Profile %s failed, trying next...", profile)
            send_ops_alert(
                f"eldo-profile:{profile}",
                f"⚠️ [ELDO] Profile `{profile}` chết cookie (capture fail) — "
                "cần re-login VNC 192.168.2.220:5900 sớm "
                "(docs/operations.md → Eldorado session re-login).",
            )
            self._next_profile()

        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        logger.error("[ELDO] All %d profiles exhausted", len(ELDO_PROFILES))
        send_ops_alert(
            "eldo-all",
            f"🔴 [ELDO] CẢ {len(ELDO_PROFILES)} profile chết cookie — bot Eldorado "
            "NGỪNG quét/giao đơn. Re-login VNC 192.168.2.220:5900 NGAY "
            "(docs/operations.md → Eldorado session re-login).",
            cooldown=2 * 3600,
        )
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
            # refresh_token countdown — time left before a manual G2G re-login is needed
            "refresh_token_exp_ms": g2g_auth.refresh_token_exp_ms,
            "refresh_token_expires_in": (
                int(max(0, g2g_auth.refresh_token_exp_ms / 1000 - time.time()))
                if g2g_auth.refresh_token_exp_ms else 0
            ),
            "refresh_last_ok_age": (
                int(time.time() - g2g_auth.refresh_last_ok)
                if g2g_auth.refresh_last_ok else None
            ),
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

    # Clean orphan browsers + stale profile locks from any previous run
    _kill_orphan_browsers()
    for prof in G2G_PROFILES + ELDO_PROFILES:
        _cleanup_profile_locks(prof)

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

            # ── Eldorado profiles — check the on-disk RefreshToken, not just
            # the directory: a logged-out profile still "exists" but is dead
            # for failover. Warn early so re-login happens BEFORE all profiles
            # are needed at once (2026-07-13: server-side revoke killed all 3).
            now = time.time()
            for profile in ELDO_PROFILES:
                is_active = (profile == eldo_auth.profile_dir)
                rt_exp = _eldo_disk_refresh_expiry(profile)
                if rt_exp is None:
                    alive = False
                    detail = "no RefreshToken on disk"
                    send_ops_alert(
                        f"eldo-disk:{profile}",
                        f"⚠️ [ELDO] Profile `{profile}` KHÔNG còn RefreshToken trên đĩa "
                        "(đã logout) — cần re-login VNC 192.168.2.220:5900.",
                    )
                elif rt_exp < now:
                    alive = False
                    detail = "RefreshToken expired"
                    send_ops_alert(
                        f"eldo-disk:{profile}",
                        f"⚠️ [ELDO] Profile `{profile}` RefreshToken trên đĩa ĐÃ HẾT HẠN — "
                        "cần re-login VNC 192.168.2.220:5900.",
                    )
                elif rt_exp - now < 72 * 3600:
                    alive = True
                    detail = f"RefreshToken expires in {(rt_exp - now) / 3600:.0f}h"
                    send_ops_alert(
                        f"eldo-disk-expiring:{profile}",
                        f"⏳ [ELDO] Profile `{profile}` RefreshToken trên đĩa còn "
                        f"{(rt_exp - now) / 3600:.0f}h — nên re-login VNC làm mới trước khi hết hạn.",
                        cooldown=24 * 3600,
                    )
                else:
                    alive = True
                    detail = f"RefreshToken OK ({(rt_exp - now) / 86400:.1f}d left)"
                    clear_ops_alert(f"eldo-disk:{profile}")
                    clear_ops_alert(f"eldo-disk-expiring:{profile}")
                profile_status[profile] = {
                    "platform": "eldo",
                    "alive": alive,
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
    _kill_orphan_browsers()
    for prof in G2G_PROFILES + ELDO_PROFILES:
        _cleanup_profile_locks(prof)
    logger.info("Auth service stopped")


def main():
    import atexit
    atexit.register(_kill_orphan_browsers)

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
