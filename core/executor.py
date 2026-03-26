"""
Playwright Executor
Maps TestStep objects → Playwright actions.
Uses a smart multi-strategy selector resolver (role, label, placeholder, text, CSS).
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Locator

from core.nlp_parser import TestStep


# ─── Result Objects ────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    step: TestStep
    passed: bool
    error: Optional[str] = None
    screenshot: Optional[str] = None       # post-action screenshot
    pre_screenshot: Optional[str] = None   # before-action screenshot
    duration_ms: float = 0.0

    def __repr__(self):
        status = "✓ PASS" if self.passed else "✗ FAIL"
        return f"{status} | Line {self.step.line_number}: {self.step}"


@dataclass
class TestResult:
    test_file: str
    steps: list[StepResult] = field(default_factory=list)
    browser: str = "chromium"

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if not s.passed)

    @property
    def duration_ms(self) -> float:
        return sum(s.duration_ms for s in self.steps)


# ─── Selector Resolver ────────────────────────────────────────────────────────

# Maps common field names → CSS / attribute selectors (tried in order)
FIELD_SELECTOR_MAP = {
    "email": [
        # Zoho accounts login page — placeholder is "Email address or mobile number"
        'input[placeholder*="Email address" i]',
        'input[placeholder*="mobile number" i]',
        'input[placeholder*="email address" i]',
        # Generic email inputs
        'input[type="email"]',
        'input[name*="email" i]',
        'input[placeholder*="email" i]',
        'input[id*="email" i]',
        '#login-account-email',
        # Last resort: first visible text input on the page
        'input[type="text"]:visible',
    ],
    "password": [
        # Zoho password screen uses placeholder "Enter password"
        'input[placeholder="Enter password"]',
        'input[placeholder*="Enter password" i]',
        'input[type="password"]',
        'input[name*="password" i]',
        'input[placeholder*="password" i]',
        'input[id*="password" i]',
    ],
    "to": [
        # Zoho Mail compose — To field (aria-label="To" is what Zoho uses)
        'input[aria-label="To"]',
        'input[aria-label*="To" i]',
        'input[id*="ZohoMail_To" i]',
        'div[id*="compose"] input[aria-label*="To" i]',
        'div[class*="compose"] input[aria-label*="To" i]',
        'input[placeholder*="Recipients" i]',
        'input[name="to"]',
        'input[placeholder*="to" i]',    # kept as fallback
        '.zc-cnt-cmt input',
        'input[id*="to" i]',
        'div[data-id="To"] input',
        '.compose-to input',
    ],
    "subject": [
        # Zoho Mail compose — subject is a plain input, often with id/name "subject"
        'input[id*="subject" i]',
        'input[name*="subject" i]',
        'input[placeholder*="subject" i]',
        'input[aria-label*="subject" i]',
        # Zoho-specific compose subject selectors
        'div[id*="compose"] input[type="text"]',
        'div[class*="compose"] input[type="text"]:not([aria-label*="To" i])',
        '.editor-subject input',
        '#subject',
    ],
    "body": [
        # Zoho Mail uses an iframe with a contenteditable body for the compose area
        # These are tried in _fill_rich_text which handles iframes specially
        'div[contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="message" i]',
        'div[contenteditable="true"][aria-label*="body" i]',
        '.mail-editor div[contenteditable]',
        '#zel_editor',
        'textarea[name*="body"]',
    ],
}

# Exact placeholder strings → field key  (used when user writes the full placeholder)
PLACEHOLDER_EXACT_MAP = {
    "email address or mobile number": "email",
    "email address":                  "email",
    "mobile number":                  "email",
    "enter password":                 "password",
    "password":                       "password",
}

BUTTON_ROLE_HINTS = ["button", "link", "menuitem", "tab", "option"]

# ── Known Zoho Mail UI element selectors ─────────────────────────────────────
# Keyed by the plain-English label used in test scripts (lowercase).
# These are tried BEFORE any fuzzy matching for known Zoho Mail actions.
ZOHO_MAIL_SELECTORS = {
    "new mail": [
        'div[id*="compose_btn" i]',
        'button[title*="New Mail" i]',
        'span[title*="New Mail" i]',
        '[aria-label*="New Mail" i]',
        'div[class*="compose"][role="button"]',
        'td[id*="newmail" i]',
        'li[id*="newmail" i]',
    ],
    "send": [
        'button[title*="Send" i]',
        '[aria-label*="Send" i]',
        'span[title*="Send" i]',
        'div[id*="send_btn" i]',
        'li[id*="sendbutton" i]',
        'div[title="Send"]',
    ],
    "my profile": [
        # Zoho Mail top-right avatar/profile menu
        'div[id*="userinfo" i]',
        'div[class*="userinfo" i]',
        'span[class*="user-name" i]',
        '[aria-label*="profile" i]',
        '[title*="profile" i]',
        'img[class*="avatar" i]',
        'div[class*="avatar" i]',
        'div[id*="profile" i]',
        'li[id*="profile" i]',
        'span[id*="userDisplayName" i]',
        '#currentUserInfo',
    ],
    "sign out": [
        # Zoho sign-out — appears after clicking profile
        'a[href*="signout" i]',
        'a[href*="logout" i]',
        '[id*="signout" i]',
        '[id*="logout" i]',
        'li[title*="Sign Out" i]',
        'span[title*="Sign Out" i]',
        '[aria-label*="Sign Out" i]',
    ],
}

# ── SSO / OAuth guard ─────────────────────────────────────────────────────────
# If the browser lands on one of these domains after a click, it has been
# hijacked by an SSO provider. The executor will navigate back and retry.
SSO_TRAP_DOMAINS = [
    "accounts.google.com",
    "login.microsoftonline.com",
    "appleid.apple.com",
    "www.facebook.com/login",
    "linkedin.com/oauth",
    "twitter.com/i/oauth",
]

# Direct native-login URLs for sites that show SSO options on their landing page.
# Key = substring present in the site's home URL.
# Value = URL for the native (non-SSO) login form.
DIRECT_LOGIN_URLS = {
    "zoho.com/mail": (
        "https://accounts.zoho.in/signin"
        "?servicename=VirtualOffice"
        "&signupurl=https://www.zoho.com/mail/signup.html"
        "&serviceurl=https://mail.zoho.in"
    ),
    "zoho.com": "https://accounts.zoho.in/signin",
}

# Selectors that identify the native "Sign in" link (not Google/Facebook SSO)
NATIVE_SIGNIN_SELECTORS = [
    # Zoho: the "Sign in" text link that opens the email form (not an OAuth icon)
    'a[href*="zoho"][href*="signin"]:not([href*="google"]):not([href*="facebook"])',
    'a[href*="accounts.zoho"]',
    # Generic native-login indicators
    'a.login-with-email',
    '[data-provider="native"]',
]


class SelectorResolver:
    """Tries multiple strategies to find the right element on a page."""

    def find_input(self, page: Page, field_name: str) -> Optional[Locator]:
        key = field_name.lower().strip()

        # 0. Exact placeholder map  ("Email address or mobile number" → "email")
        resolved_key = PLACEHOLDER_EXACT_MAP.get(key, key)

        # 1. Known field map (tries resolved key first, then original)
        for lookup_key in dict.fromkeys([resolved_key, key]):   # deduplicated, ordered
            if lookup_key in FIELD_SELECTOR_MAP:
                for sel in FIELD_SELECTOR_MAP[lookup_key]:
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible(timeout=1500):
                            print(f"    ↳ found via selector map [{lookup_key}]: {sel}")
                            return loc
                    except Exception:
                        pass

        # 2. get_by_placeholder — exact substring match on the placeholder attribute
        #    Tries the raw field_name string (user may have typed the placeholder literally)
        for placeholder_try in [field_name, key]:
            try:
                loc = page.get_by_placeholder(placeholder_try, exact=False)
                if loc.count() > 0 and loc.first.is_visible(timeout=1500):
                    print(f"    ↳ found via get_by_placeholder: {placeholder_try!r}")
                    return loc.first
            except Exception:
                pass

        # 3. get_by_label — ARIA label
        try:
            loc = page.get_by_label(field_name, exact=False)
            if loc.count() > 0 and loc.first.is_visible(timeout=1500):
                print(f"    ↳ found via get_by_label: {field_name!r}")
                return loc.first
        except Exception:
            pass

        # 4. aria-label / aria-describedby attribute substring
        escaped = field_name.replace('"', '\\"')
        for attr_sel in [
            f'input[aria-label*="{escaped}" i]',
            f'input[aria-describedby*="{escaped}" i]',
            f'[role="textbox"][aria-label*="{escaped}" i]',
        ]:
            try:
                loc = page.locator(attr_sel).first
                if loc.is_visible(timeout=1500):
                    print(f"    ↳ found via aria attr: {attr_sel}")
                    return loc
            except Exception:
                pass

        # 5. Generic input[name/id/class] substring
        for attr in ["name", "id", "class"]:
            try:
                loc = page.locator(f'input[{attr}*="{key}" i]').first
                if loc.is_visible(timeout=1500):
                    print(f"    ↳ found via input[{attr}*=...]: {key}")
                    return loc
            except Exception:
                pass

        # 6. Last resort — scan ALL visible inputs and fuzzy-match their placeholder / label
        try:
            all_inputs = page.locator(
                'input[type="text"], input[type="email"], input:not([type])'
            ).all()
            from rapidfuzz import fuzz
            best_loc, best_score = None, 0
            for inp in all_inputs:
                if not _safe_visible(inp):
                    continue
                candidates = []
                for attr in ["placeholder", "aria-label", "name", "id"]:
                    try:
                        val = inp.get_attribute(attr) or ""
                        if val:
                            candidates.append(val)
                    except Exception:
                        pass
                for candidate in candidates:
                    score = fuzz.partial_ratio(key, candidate.lower())
                    if score > best_score:
                        best_score = score
                        best_loc = inp
            if best_score >= 60 and best_loc is not None:
                print(f"    ↳ found via fuzzy input scan (score={best_score})")
                return best_loc
        except Exception:
            pass

        return None

    def find_button(self, page: Page, text: str) -> Optional[Locator]:
        text_stripped = text.strip()
        text_lower = text_stripped.lower()
        escaped = text_stripped.replace('"', '\\"')

        print(f"    [find_button] looking for: {text_stripped!r}")

        # ── PASS 0: Known Zoho Mail UI element map ────────────────────────────
        if text_lower in ZOHO_MAIL_SELECTORS:
            for sel in ZOHO_MAIL_SELECTORS[text_lower]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1000):
                        print(f"    ↳ Zoho UI map: {sel}")
                        return loc
                except Exception:
                    pass

        # ── PASS 1: Exact text matches (highest confidence) ──────────────────

        # 1a. Exact role match
        for role in BUTTON_ROLE_HINTS:
            try:
                loc = page.get_by_role(role, name=text_stripped, exact=True)  # type: ignore
                if loc.count() > 0 and loc.first.is_visible(timeout=800):
                    print(f"    ↳ exact role[{role}] match")
                    return loc.first
            except Exception:
                pass

        # 1b. input[type=submit] or input[type=button] with matching value
        for sel in [
            f'input[type="submit"][value="{escaped}"]',
            f'input[type="button"][value="{escaped}"]',
            f'button[type="submit"]:has-text("{escaped}")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    print(f"    ↳ exact submit/button: {sel}")
                    return loc
            except Exception:
                pass

        # 1c. Exact text content CSS match
        for sel in [
            f'button:text-is("{escaped}")',
            f'a:text-is("{escaped}")',
            f'[role="button"]:text-is("{escaped}")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    print(f"    ↳ exact text-is: {sel}")
                    return loc
            except Exception:
                pass

        # ── PASS 2: Partial / has-text matches ───────────────────────────────

        # 2a. Inexact role match
        for role in BUTTON_ROLE_HINTS:
            try:
                loc = page.get_by_role(role, name=text_stripped, exact=False)  # type: ignore
                if loc.count() > 0:
                    # If multiple matches, pick the one whose text best matches
                    all_locs = [l for l in loc.all() if _safe_visible(l)]
                    if len(all_locs) == 1:
                        print(f"    ↳ inexact role[{role}] — single match")
                        return all_locs[0]
                    elif len(all_locs) > 1:
                        # Pick the one with the shortest text (most exact match)
                        best = min(all_locs, key=lambda l: abs(len(_safe_text(l)) - len(text_stripped)))
                        print(f"    ↳ inexact role[{role}] — {len(all_locs)} matches, picked shortest")
                        return best
            except Exception:
                pass

        # 2b. :has-text CSS (contains match)
        for sel in [
            f'button:has-text("{escaped}")',
            f'input[type="submit"][value*="{escaped}" i]',
            f'a:has-text("{escaped}")',
            f'[class*="btn"]:has-text("{escaped}")',
            f'[role="button"]:has-text("{escaped}")',
            f'span:has-text("{escaped}")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    print(f"    ↳ has-text: {sel}")
                    return loc
            except Exception:
                pass

        # 2c. get_by_text exact=True
        try:
            loc = page.get_by_text(text_stripped, exact=True)
            visible = [l for l in loc.all() if _safe_visible(l)]
            if visible:
                print(f"    ↳ get_by_text exact=True")
                return visible[0]
        except Exception:
            pass

        # ── PASS 3: Fuzzy scoring — scored ranking across ALL candidates ─────
        # Scores every visible interactive element; picks best match above threshold.
        # Uses token_sort_ratio so "Sign in" scores higher than "Try smart sign-in".
        try:
            from rapidfuzz import fuzz
            candidates = page.locator(
                "button, a, input[type='submit'], input[type='button'], "
                "[role='button'], [role='link'], [role='menuitem']"
            ).all()

            scored = []
            for c in candidates:
                if not _safe_visible(c):
                    continue
                c_text = _safe_text(c) or _safe_attr(c, "value") or ""
                if not c_text:
                    continue

                # Use multiple scoring methods; take the best
                ratio       = fuzz.ratio(text_lower, c_text.lower())
                token_sort  = fuzz.token_sort_ratio(text_lower, c_text.lower())
                partial     = fuzz.partial_ratio(text_lower, c_text.lower())

                # Heavily reward short candidates whose text is close to query
                # Penalise long candidates (e.g. "Try smart sign-in" vs "Sign in")
                length_penalty = max(0, len(c_text) - len(text_stripped)) * 3.0
                best_raw = max(ratio, token_sort, partial)
                final = best_raw - length_penalty

                scored.append((final, best_raw, c, c_text))

            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                top_score, raw_score, top_loc, top_text = scored[0]
                print(f"    ↳ fuzzy: best={top_text!r} final={top_score:.1f} raw={raw_score}")
                if top_score >= 60:
                    return top_loc
        except Exception:
            pass

        print(f"    ✗ find_button: no match found for {text_stripped!r}")
        return None


def _safe_visible(loc: Locator) -> bool:
    try:
        return loc.is_visible(timeout=500)
    except Exception:
        return False


def _safe_text(loc: Locator) -> str:
    try:
        return loc.inner_text().strip()
    except Exception:
        return ""


def _safe_attr(loc: Locator, attr: str) -> str:
    try:
        return (loc.get_attribute(attr) or "").strip()
    except Exception:
        return ""


def _fuzzy_score(query: str, candidate: str) -> int:
    from rapidfuzz import fuzz
    return fuzz.ratio(query.lower(), candidate.lower())


def _extract_domain(url: str) -> str:
    """Extract bare hostname from a URL, e.g. 'mail.zoho.in' → 'zoho.in'."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""


# ─── Executor ─────────────────────────────────────────────────────────────────

class PlaywrightExecutor:
    def __init__(self, browser_type: str = "chromium", headless: bool = False,
                 slow_mo: int = 600, timeout: int = 15000,
                 screenshot_dir: str = "reports/screenshots"):
        self.browser_type = browser_type
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self.screenshot_dir = screenshot_dir
        self.resolver = SelectorResolver()
        self._compose_ready = False   # cached: True once compose window confirmed open
        import os
        os.makedirs(screenshot_dir, exist_ok=True)

    def run(self, steps: list[TestStep], test_name: str) -> TestResult:
        result = TestResult(test_file=test_name, browser=self.browser_type)

        with sync_playwright() as p:
            browser_factory = getattr(p, self.browser_type)
            browser: Browser = browser_factory.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=["--start-maximized"],
            )
            context: BrowserContext = browser.new_context(
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
            )
            page: Page = context.new_page()
            page.set_default_timeout(self.timeout)

            for step in steps:
                step_result = self._execute_step(page, step, test_name)
                result.steps.append(step_result)
                if not step_result.passed:
                    # Take failure screenshot and continue (or break — configurable)
                    print(f"  ⚠  Step failed at line {step.line_number}: {step.raw}")
                    # break  # Uncomment to stop on first failure

            browser.close()

        return result

    # ── Step Dispatch ─────────────────────────────────────────────────────────

    def _execute_step(self, page: Page, step: TestStep, test_name: str) -> StepResult:
        start = time.time()
        screenshot_path = None
        error = None
        passed = True

        # ── PRE-STEP screenshot (before action) ───────────────────────────────
        pre_shot = self._take_screenshot(page, test_name, step.line_number, "before")

        try:
            action = step.action
            if action == "navigate":
                self._do_navigate(page, step)
            elif action == "click":
                self._do_click(page, step)
            elif action == "fill":
                self._do_fill(page, step)
            elif action == "wait":
                self._do_wait(page, step)
            elif action == "assert":
                self._do_assert(page, step)
            elif action == "scroll":
                self._do_scroll(page, step)
            elif action == "hover":
                self._do_hover(page, step)
            elif action == "unknown":
                print(f"  ℹ  Skipping unrecognized step: {step.raw!r}")
            else:
                raise ValueError(f"Unknown action: {action!r}")

        except Exception as e:
            passed = False
            error = str(e)

        # ── POST-STEP screenshot (after action, pass or fail) ─────────────────
        post_shot = self._take_screenshot(page, test_name, step.line_number,
                                          "after" if passed else "FAIL")

        # Primary screenshot for the report = post-step state
        screenshot_path = post_shot or pre_shot

        duration_ms = (time.time() - start) * 1000

        if not passed:
            print(f"  ⚠  Step failed at line {step.line_number}: {step.raw}")

        return StepResult(
            step=step,
            passed=passed,
            error=error,
            screenshot=screenshot_path,
            pre_screenshot=pre_shot,
            duration_ms=duration_ms,
        )

    def _take_screenshot(self, page: Page, test_name: str,
                         line_num: int, label: str) -> Optional[str]:
        """Capture a full-page screenshot and return its path, or None on error."""
        try:
            # Sanitise test_name for use in filename
            safe_name = re.sub(r"[^\w\-]", "_", test_name)
            path = (f"{self.screenshot_dir}/"
                    f"{safe_name}_step{line_num:02d}_{label}.png")
            page.screenshot(path=path, full_page=False)
            return path
        except Exception as e:
            print(f"    ⚠  Screenshot failed ({label}): {e!s:.50}")
            return None

    # ── Action Implementations ─────────────────────────────────────────────────

    def _do_navigate(self, page: Page, step: TestStep):
        url = step.value or ""
        if not url.startswith("http"):
            url = "https://" + url
        self._compose_ready = False   # new page = compose not open yet
        page.goto(url, wait_until="domcontentloaded")
        self._escape_sso_if_needed(page, url)

    def _do_click(self, page: Page, step: TestStep):
        target = step.target or ""
        origin_url = page.url
        origin_lower = origin_url.lower()

        # ── Context detection ─────────────────────────────────────────────────
        on_zoho_accounts = "accounts.zoho" in origin_lower
        on_sso_landing   = not on_zoho_accounts and any(
            kw in target.lower() for kw in ["sign in", "signin", "log in", "login"]
        )

        if on_sso_landing:
            print(f"  [click] SSO-landing mode → running native-signin finder for {target!r}")
            loc = self._find_native_signin(page, target)
        else:
            loc = None

        if loc is None:
            loc = self.resolver.find_button(page, target)

        if loc is None:
            raise RuntimeError(f"Cannot find clickable element: {target!r}")

        loc.scroll_into_view_if_needed()

        # Reset compose-window cache if opening a new compose window
        if any(kw in target.lower() for kw in ["new mail", "compose", "new message"]):
            self._compose_ready = False

        # Use force=True as fallback if normal click is blocked
        try:
            loc.click(timeout=8000)
        except Exception as e:
            print(f"    ⚠  Click failed ({e!s:.60}), retrying force=True")
            loc.click(force=True)

        self._wait_for_post_click_transition(page, target)
        self._escape_sso_if_needed(page, origin_url)

    def _wait_for_post_click_transition(self, page: Page, clicked_target: str):
        """
        After a click, wait for the most likely next UI state to settle.

        For "Next"-style buttons on login forms: Zoho replaces the email input
        with a password panel via CSS transition (~400 ms). We must wait for
        that DOM change before the next step tries to interact with the page.

        Strategy (in order):
          1. If we clicked something that looks like a "Next/Continue/Submit"
             button on a login page → wait for a password field to appear.
          2. Otherwise wait for network to go idle (covers full navigations).
          3. Always wait for any pending CSS animations to finish.
        """
        target_lower = clicked_target.lower()
        is_next_step = any(kw in target_lower for kw in
                           ["next", "continue", "proceed", "submit"])
        is_signin_step = any(kw in target_lower for kw in
                             ["sign in", "signin", "login", "log in"])

        if is_next_step:
            # Zoho: after clicking "Next" the password input slides in.
            # Wait up to 8 s for input[type=password] to become visible.
            print(f"    ↳ 'Next' clicked — waiting for password field to appear…")
            try:
                page.wait_for_selector(
                    'input[type="password"], input[placeholder*="password" i], '
                    'input[placeholder*="Enter password" i]',
                    state="visible",
                    timeout=8000,
                )
                print("    ✓ Password field appeared")
                return
            except Exception:
                pass  # Not a password step — fall through to generic wait

        if is_signin_step:
            # After "Sign in" click — wait for either a dashboard/inbox indicator
            # or a new page navigation to complete.
            print(f"    ↳ Sign-in clicked — waiting for page transition…")
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
                return
            except Exception:
                pass

        # Generic: wait for DOM to stop changing (covers JS-rendered transitions)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        # Extra buffer for CSS slide/fade animations (Zoho uses ~300-400 ms transitions)
        page.wait_for_timeout(400)

    # ── SSO helpers ───────────────────────────────────────────────────────────

    def _is_sso_trapped(self, page: Page) -> bool:
        """Return True if current URL is an external SSO provider page."""
        current = page.url.lower()
        return any(trap in current for trap in SSO_TRAP_DOMAINS)

    def _escape_sso_if_needed(self, page: Page, origin_url: str):
        """
        If we've been redirected to an SSO provider, navigate back to the
        direct native-login URL for the originating site.
        """
        if not self._is_sso_trapped(page):
            return

        print(f"  ⚠  SSO redirect detected → {page.url}")
        print(f"     Looking for direct login URL for: {origin_url}")

        # Find the best matching direct-login URL
        for key, direct_url in DIRECT_LOGIN_URLS.items():
            if key in origin_url.lower():
                print(f"  ↩  Navigating to direct login: {direct_url}")
                page.goto(direct_url, wait_until="domcontentloaded")
                return

        # Fallback: just go back
        print("  ↩  No direct URL found — pressing Back")
        page.go_back(wait_until="domcontentloaded")

    def _find_native_signin(self, page: Page, target_text: str) -> Optional[Locator]:
        """
        For sign-in clicks on SSO-landing pages: find the native Zoho login
        link rather than a Google/Facebook SSO button.

        Scoring:
          text_ratio        — how closely button text matches target
          length_penalty    — penalise longer strings (e.g. "Sign in with Google")
          sso_penalty       — hard penalty if href points to external SSO domain
          domain_bonus      — bonus if href stays on Zoho accounts domain
        """
        from rapidfuzz import fuzz

        # 1. Try explicit native-login CSS selectors first
        for sel in NATIVE_SIGNIN_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    print(f"    ↳ native sign-in via selector: {sel}")
                    return loc
            except Exception:
                pass

        # 2. Score ALL visible interactive elements
        try:
            current_domain = _extract_domain(page.url)
            candidates = page.locator(
                "a, button, [role='button'], [role='link']"
            ).all()

            scored = []
            for c in candidates:
                if not _safe_visible(c):
                    continue
                text = _safe_text(c)
                href = _safe_attr(c, "href")
                if not text:
                    continue

                # Base text similarity
                text_ratio = fuzz.ratio(target_text.lower(), text.lower())
                if text_ratio < 50:
                    continue

                # Length penalty: longer text = lower score
                # "Sign in with Google" (19 chars) vs "Sign in" (7 chars) → −36 penalty
                length_penalty = max(0, len(text) - len(target_text)) * 3.0

                # SSO penalty: −80 if href points to external auth
                sso_penalty = 0
                for trap in SSO_TRAP_DOMAINS:
                    if trap in href.lower():
                        sso_penalty = 80
                        break

                # Domain bonus: +25 if link stays on Zoho
                domain_bonus = 0
                if "accounts.zoho" in href:
                    domain_bonus = 25
                elif current_domain and current_domain in href:
                    domain_bonus = 10

                final = text_ratio - length_penalty - sso_penalty + domain_bonus
                scored.append((final, text_ratio, c, text, href))
                print(f"    [native-signin] {text!r:<30} ratio={text_ratio:.0f}  "
                      f"len_pen={length_penalty:.0f}  sso_pen={sso_penalty}  "
                      f"dom_bon={domain_bonus}  FINAL={final:.1f}")

            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                best_final, best_raw, best_loc, best_text, best_href = scored[0]
                print(f"    ↳ native sign-in WINNER: {best_text!r} "
                      f"(final={best_final:.1f}, href={best_href[:50]})")
                if best_final > 40:   # only accept if clearly positive score
                    return best_loc
        except Exception as e:
            print(f"    ⚠  _find_native_signin error: {e}")

        return None

    def _do_fill(self, page: Page, step: TestStep):
        target = step.target or ""
        value = step.value or ""

        # Special: rich-text body editor (iframe or contenteditable)
        if target.lower() in ("body", "message body", "message"):
            self._fill_rich_text(page, value)
            return

        # Special: "To" / recipient fields in Zoho Mail compose
        # These use a token-input widget: typing shows a dropdown, and the
        # address must be CONFIRMED with Enter/Tab to become a real token.
        # If not confirmed, Zoho ignores the text and shows "Please enter a
        # recipient's address" when Send is clicked.
        if target.lower() in ("to", "to email", "to email field", "recipient"):
            self._fill_recipient_field(page, value)
            return

        # Wait for the field to be in the DOM and visible
        self._wait_for_field_visible(page, target)

        loc = self.resolver.find_input(page, target)
        if loc is None:
            raise RuntimeError(f"Cannot find input field: {target!r}")

        loc.scroll_into_view_if_needed()

        # Try normal click first, fall back to force=True if element is
        # technically visible but blocked by an animation or overlay
        try:
            loc.click(timeout=5000)
        except Exception as click_err:
            print(f"    ⚠  Normal click failed ({click_err!s:.60}), retrying with force=True")
            try:
                loc.click(force=True, timeout=5000)
            except Exception:
                print(f"    ⚠  Force click failed — using JS focus")
                page.evaluate("el => el.focus()", loc.element_handle())

        # Clear existing content then type new value
        try:
            loc.fill(value)
        except Exception:
            print(f"    ⚠  fill() failed — using triple-click + type")
            loc.click(force=True)
            page.keyboard.press("Control+a")
            page.keyboard.type(value)

    def _fill_recipient_field(self, page: Page, email: str):
        """
        Fill a token-input recipient field (Zoho Mail 'To' box).

        Strategy:
          1. Wait for compose window + To input to be ready
          2. Click the To input area
          3. Type the email address character by character (delay=50 triggers autocomplete)
          4. Wait for autocomplete dropdown — try selectors sequentially
          5. Click the matching suggestion if found
          6. Fallback: press Enter to commit raw typed address as token
          7. Press Escape to dismiss any remaining dropdown
        """
        print(f"    ↳ Filling recipient field with: {email!r}")

        # 1. Wait for compose window (uses cache after first call)
        self._wait_for_field_visible(page, "to")

        # 2. Find and click the To input
        loc = self.resolver.find_input(page, "to")
        if loc is None:
            raise RuntimeError("Cannot find To/recipient input field")

        loc.scroll_into_view_if_needed()
        try:
            loc.click(timeout=5000)
        except Exception:
            loc.click(force=True)

        # 3. Clear any existing content and type the email
        try:
            loc.fill("")
        except Exception:
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")

        # Type with delay so the autocomplete widget fires
        page.keyboard.type(email, delay=50)
        print(f"    ↳ Typed email, waiting for autocomplete…")

        # 4. Wait for autocomplete dropdown — try selectors sequentially
        for dropdown_sel in [
            f'li:has-text("{email}")',
            f'[class*="autocomplete"] li',
            f'[class*="suggestion"]',
            f'[role="option"]:has-text("{email.split("@")[0]}")',
            f'[role="listbox"]',
        ]:
            try:
                page.wait_for_selector(dropdown_sel, state="visible", timeout=2000)
                print(f"    ✓ Autocomplete dropdown appeared: {dropdown_sel}")
                # 5. Click the matching suggestion
                suggestion = page.locator(dropdown_sel).first
                if suggestion.is_visible(timeout=500):
                    suggestion.click()
                    print(f"    ✓ Clicked autocomplete suggestion")
                    page.wait_for_timeout(300)
                    return
                break
            except Exception:
                pass

        # 6. No autocomplete — press Enter to commit the typed address as a token
        print(f"    ↳ Pressing Enter to commit recipient token")
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)

        # 7. Dismiss any remaining dropdown
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

    def _wait_for_field_visible(self, page: Page, field_name: str):
        """
        Wait for a field to be visible. Races all candidate selectors in parallel.
        Compose-window detection is cached — only runs once per compose session.
        """
        key = field_name.lower().strip()

        # ── Compose-window fields: wait for panel ONCE, then cache ────────────
        if key in ("to", "subject", "body", "message body", "message"):
            if not self._compose_ready:
                print(f"    ↳ Waiting for compose window to open…")
                compose_combined = ", ".join([
                    'div[id*="compose"]',
                    'div[class*="compose"]',
                    '.editor-subject',
                    'input[id*="subject" i]',
                    'input[name*="subject" i]',
                    'input[aria-label*="subject" i]',
                ])
                try:
                    page.wait_for_selector(compose_combined, state="visible", timeout=8000)
                    self._compose_ready = True
                    print(f"    ✓ Compose window ready")
                    page.wait_for_timeout(300)
                except Exception:
                    print(f"    ⚠  Compose window not confirmed — proceeding")
            else:
                print(f"    ↳ Compose window already open (cached)")

        # ── Race ALL field selectors simultaneously ────────────────────────────
        wait_selectors = []
        if key in FIELD_SELECTOR_MAP:
            wait_selectors.extend(FIELD_SELECTOR_MAP[key][:4])

        if "password" in key:
            wait_selectors = ['input[type="password"]',
                              'input[placeholder*="Enter password" i]'] + wait_selectors
        elif "email" in key or "mobile" in key:
            wait_selectors = ['input[placeholder*="Email address" i]',
                              'input[type="email"]'] + wait_selectors

        if not wait_selectors:
            return

        combined = ", ".join(dict.fromkeys(wait_selectors))
        try:
            page.wait_for_selector(combined, state="visible", timeout=5000)
            print(f"    ↳ field '{field_name}' is now visible")
        except Exception:
            print(f"    ⚠  field '{field_name}' not confirmed visible — proceeding")

    def _fill_rich_text(self, page: Page, value: str):
        """Try iframe body first, then contenteditable div."""
        # Try iframe
        frames = page.frames
        for frame in frames:
            try:
                body = frame.locator("body[contenteditable], body")
                if body.is_visible(timeout=1000):
                    body.click()
                    body.fill(value)
                    return
            except Exception:
                pass

        # Try contenteditable div
        for sel in ['div[contenteditable="true"]', '[contenteditable]', '.note-editable']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.click()
                    loc.fill(value)
                    return
            except Exception:
                pass

        raise RuntimeError("Cannot find rich-text body editor")

    def _do_wait(self, page: Page, step: TestStep):
        condition = step.condition or ""
        # Look for the condition text to appear somewhere on page
        try:
            page.wait_for_selector(f"text={condition}", timeout=self.timeout)
        except Exception:
            # Fallback: wait for any element containing the text
            page.wait_for_function(
                f"document.body.innerText.toLowerCase().includes({condition.lower()!r})",
                timeout=self.timeout,
            )

    def _do_assert(self, page: Page, step: TestStep):
        condition = step.condition or ""
        content = page.content().lower()
        if condition.lower() not in content:
            # Also check visible text
            visible = page.inner_text("body").lower()
            if condition.lower() not in visible:
                raise AssertionError(f"Expected to find {condition!r} on page")

    def _do_scroll(self, page: Page, step: TestStep):
        target = (step.target or "down").lower()
        if "up" in target:
            page.evaluate("window.scrollBy(0, -500)")
        elif "down" in target:
            page.evaluate("window.scrollBy(0, 500)")
        else:
            # Scroll to element
            loc = self.resolver.find_button(page, target)
            if loc:
                loc.scroll_into_view_if_needed()

    def _do_hover(self, page: Page, step: TestStep):
        target = step.target or ""
        loc = self.resolver.find_button(page, target)
        if loc is None:
            raise RuntimeError(f"Cannot find hover target: {target!r}")
        loc.hover()
