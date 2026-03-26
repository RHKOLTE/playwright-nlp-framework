"""
Playwright Executor — Generic, Self-Learning
============================================
All selector maps are loaded from KnowledgeStore (knowledge/knowledge.json).
Nothing is hardcoded. The framework learns from every run.
"""

import re
import time
import json
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Locator

from core.nlp_parser import TestStep
from core.knowledge import get_store, KnowledgeStore


# ─── Result Objects ────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    step: TestStep
    passed: bool
    error: Optional[str] = None
    screenshot: Optional[str] = None
    pre_screenshot: Optional[str] = None
    duration_ms: float = 0.0
    dom_snapshot: Optional[dict] = None

    def __repr__(self):
        status = "✓ PASS" if self.passed else "✗ FAIL"
        return f"{status} | Line {self.step.line_number}: {self.step}"


@dataclass
class TestResult:
    test_file: str
    run_id: str
    steps: list[StepResult] = field(default_factory=list)
    browser: str = "chromium"
    video_path: Optional[str] = None

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


# ─── DOM Snapshot ─────────────────────────────────────────────────────────────

def capture_dom_snapshot(page: Page) -> dict:
    """
    Capture a lightweight DOM fingerprint — inputs, buttons, URL, title.
    Saved to knowledge store for regression comparison between runs.
    """
    try:
        snapshot = page.evaluate("""() => {
            const getAttrs = el => ({
                tag:         el.tagName.toLowerCase(),
                type:        el.type || '',
                id:          el.id || '',
                name:        el.name || '',
                placeholder: el.placeholder || '',
                aria_label:  el.getAttribute('aria-label') || '',
                label:       el.labels && el.labels[0] ? el.labels[0].textContent.trim() : '',
                visible:     el.offsetParent !== null,
            });
            const inputs   = [...document.querySelectorAll('input,textarea,select')]
                              .filter(e => e.offsetParent !== null)
                              .map(getAttrs);
            const buttons  = [...document.querySelectorAll(
                                'button,a,[role="button"],[role="link"]')]
                              .filter(e => e.offsetParent !== null)
                              .map(e => e.innerText.trim().slice(0,60))
                              .filter(Boolean);
            return {
                url:     location.href,
                title:   document.title,
                inputs:  inputs.slice(0, 30),
                buttons: [...new Set(buttons)].slice(0, 50),
            };
        }""")
        return snapshot
    except Exception:
        return {"url": page.url, "title": "", "inputs": [], "buttons": []}


# ─── Selector Resolver ────────────────────────────────────────────────────────

class SelectorResolver:
    """
    Generic element finder. All selector knowledge comes from KnowledgeStore —
    no hardcoded maps. Learns from every successful/failed resolution.
    """

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def find_input(self, page: Page, field_name: str) -> Optional[Locator]:
        key = field_name.lower().strip()

        # 0. Resolve placeholder alias ("Email address or mobile number" → "email")
        resolved_key = self.store.placeholder_map.get(key, key)

        # 1. Knowledge store field map (best selectors first, promoted by learning)
        for lookup_key in dict.fromkeys([resolved_key, key]):
            selectors = self.store.field_selectors.get(lookup_key, [])
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1500):
                        print(f"    ↳ found via knowledge map [{lookup_key}]: {sel}")
                        self.store.record_hit(sel, lookup_key)
                        return loc
                    else:
                        self.store.record_miss(sel)
                except Exception:
                    self.store.record_miss(sel)

        # 2. get_by_placeholder (Playwright built-in)
        for placeholder_try in [field_name, key]:
            try:
                loc = page.get_by_placeholder(placeholder_try, exact=False)
                if loc.count() > 0 and loc.first.is_visible(timeout=1500):
                    print(f"    ↳ found via get_by_placeholder: {placeholder_try!r}")
                    # Learn: add placeholder selector to knowledge
                    learned_sel = f'input[placeholder*="{placeholder_try}" i]'
                    self.store.learn_field_selector(resolved_key, learned_sel)
                    self.store.learn_placeholder(placeholder_try, resolved_key)
                    return loc.first
            except Exception:
                pass

        # 3. get_by_label (ARIA)
        try:
            loc = page.get_by_label(field_name, exact=False)
            if loc.count() > 0 and loc.first.is_visible(timeout=1500):
                print(f"    ↳ found via get_by_label: {field_name!r}")
                learned_sel = f'[aria-label*="{field_name}" i]'
                self.store.learn_field_selector(resolved_key, learned_sel)
                return loc.first
        except Exception:
            pass

        # 4. aria-label / aria-describedby attribute
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
                    self.store.learn_field_selector(resolved_key, attr_sel)
                    return loc
            except Exception:
                pass

        # 5. name/id/class substring
        for attr in ["name", "id", "class"]:
            try:
                loc = page.locator(f'input[{attr}*="{key}" i]').first
                if loc.is_visible(timeout=1500):
                    sel = f'input[{attr}*="{key}" i]'
                    print(f"    ↳ found via input[{attr}*=...]: {key}")
                    self.store.learn_field_selector(resolved_key, sel)
                    return loc
            except Exception:
                pass

        # 6. Fuzzy scan — inspect all visible inputs, match by attributes
        try:
            from rapidfuzz import fuzz
            all_inputs = page.locator(
                'input[type="text"], input[type="email"], input[type="tel"], input:not([type])'
            ).all()
            best_loc, best_score, best_attr_val = None, 0, ""
            for inp in all_inputs:
                if not _safe_visible(inp):
                    continue
                for attr in ["placeholder", "aria-label", "name", "id"]:
                    try:
                        val = inp.get_attribute(attr) or ""
                        if val:
                            score = fuzz.partial_ratio(key, val.lower())
                            if score > best_score:
                                best_score = score
                                best_loc = inp
                                best_attr_val = val
                    except Exception:
                        pass
            if best_score >= 60 and best_loc is not None:
                print(f"    ↳ found via fuzzy scan (score={best_score}, matched={best_attr_val!r})")
                # Learn this selector
                for attr in ["id", "name", "aria-label", "placeholder"]:
                    try:
                        val = best_loc.get_attribute(attr)
                        if val:
                            learned = f'input[{attr}="{val}"]'
                            self.store.learn_field_selector(resolved_key, learned)
                            break
                    except Exception:
                        pass
                return best_loc
        except Exception:
            pass

        return None

    def find_button(self, page: Page, text: str) -> Optional[Locator]:
        text_stripped = text.strip()
        text_lower = text_stripped.lower()
        escaped = text_stripped.replace('"', '\\"')

        print(f"    [find_button] looking for: {text_stripped!r}")

        # 0. Knowledge store button map
        if text_lower in self.store.button_selectors:
            for sel in self.store.button_selectors[text_lower]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1000):
                        print(f"    ↳ knowledge button map: {sel}")
                        self.store.record_hit(sel)
                        return loc
                    else:
                        self.store.record_miss(sel)
                except Exception:
                    self.store.record_miss(sel)

        # 1. Exact role match
        for role in ["button", "link", "menuitem", "tab", "option"]:
            try:
                loc = page.get_by_role(role, name=text_stripped, exact=True)  # type: ignore
                if loc.count() > 0 and loc.first.is_visible(timeout=800):
                    print(f"    ↳ exact role[{role}] match")
                    self._learn_button(page, loc.first, text_lower)
                    return loc.first
            except Exception:
                pass

        # 2. Exact submit input
        for sel in [
            f'input[type="submit"][value="{escaped}"]',
            f'button[type="submit"]:has-text("{escaped}")',
            f'button:text-is("{escaped}")',
            f'a:text-is("{escaped}")',
            f'[role="button"]:text-is("{escaped}")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    print(f"    ↳ exact text-is: {sel}")
                    self._learn_button(page, loc, text_lower)
                    return loc
            except Exception:
                pass

        # 3. Inexact role — pick shortest text match
        for role in ["button", "link", "menuitem", "tab", "option"]:
            try:
                loc = page.get_by_role(role, name=text_stripped, exact=False)  # type: ignore
                if loc.count() > 0:
                    all_locs = [l for l in loc.all() if _safe_visible(l)]
                    if all_locs:
                        best = min(all_locs, key=lambda l: abs(len(_safe_text(l)) - len(text_stripped)))
                        print(f"    ↳ inexact role[{role}]")
                        self._learn_button(page, best, text_lower)
                        return best
            except Exception:
                pass

        # 4. :has-text CSS
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
                    self._learn_button(page, loc, text_lower)
                    return loc
            except Exception:
                pass

        # 5. Fuzzy scoring with length penalty
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
                ratio      = fuzz.ratio(text_lower, c_text.lower())
                token_sort = fuzz.token_sort_ratio(text_lower, c_text.lower())
                partial    = fuzz.partial_ratio(text_lower, c_text.lower())
                length_penalty = max(0, len(c_text) - len(text_stripped)) * 3.0
                best_raw = max(ratio, token_sort, partial)
                final = best_raw - length_penalty
                scored.append((final, best_raw, c, c_text))

            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                top_score, raw_score, top_loc, top_text = scored[0]
                print(f"    ↳ fuzzy: best={top_text!r} final={top_score:.1f}")
                if top_score >= 60:
                    self._learn_button(page, top_loc, text_lower)
                    return top_loc
        except Exception:
            pass

        print(f"    ✗ find_button: no match found for {text_stripped!r}")
        return None

    def _learn_button(self, page: Page, loc: Locator, label: str):
        """Learn a stable CSS selector for a successfully found button."""
        try:
            for attr in ["id", "aria-label", "title", "data-testid"]:
                val = loc.get_attribute(attr)
                if val:
                    sel = f'[{attr}="{val}"]'
                    self.store.learn_button_selector(label, sel)
                    return
            # Fallback: use tag + class fragment
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            cls = loc.get_attribute("class") or ""
            first_cls = cls.split()[0] if cls else ""
            if first_cls:
                sel = f'{tag}.{first_cls}'
                self.store.learn_button_selector(label, sel)
        except Exception:
            pass


# ─── Helpers ──────────────────────────────────────────────────────────────────

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

def _extract_domain(url: str) -> str:
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
                 record_video: bool = False,
                 reports_base: str = "reports"):
        self.browser_type = browser_type
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self.record_video = record_video
        self.reports_base = reports_base
        self.store = get_store()
        self.resolver = SelectorResolver(self.store)
        self._compose_ready = False

    def run(self, steps: list[TestStep], test_name: str, run_id: str) -> TestResult:
        """
        Execute a list of parsed steps.
        run_id   — UTC datetime string used for folder naming
        test_name — test file name, used for folder naming
        """
        # ── Build run folder: reports/<test_name>/<run_id>/ ──────────────────
        safe_test = re.sub(r"[^\w\-]", "_", Path(test_name).stem)
        run_dir   = Path(self.reports_base) / safe_test / run_id
        shot_dir  = run_dir / "screenshots"
        video_dir = run_dir / "video"
        shot_dir.mkdir(parents=True, exist_ok=True)

        self.run_dir  = run_dir
        self.shot_dir = shot_dir
        self.run_id   = run_id

        result = TestResult(test_file=test_name, run_id=run_id, browser=self.browser_type)

        with sync_playwright() as p:
            browser_factory = getattr(p, self.browser_type)
            browser: Browser = browser_factory.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=["--start-maximized"],
            )

            # Video recording context
            ctx_options: dict = {
                "viewport": {"width": 1440, "height": 900},
                "ignore_https_errors": True,
            }
            if self.record_video:
                video_dir.mkdir(parents=True, exist_ok=True)
                ctx_options["record_video_dir"] = str(video_dir)
                ctx_options["record_video_size"] = {"width": 1440, "height": 900}
                print(f"  🎬 Video recording enabled → {video_dir}")

            context: BrowserContext = browser.new_context(**ctx_options)
            page: Page = context.new_page()
            page.set_default_timeout(self.timeout)

            for step in steps:
                step_result = self._execute_step(page, step, safe_test)
                result.steps.append(step_result)
                if not step_result.passed:
                    print(f"  ⚠  Step failed at line {step.line_number}: {step.raw}")

            # Close context (flushes video)
            context.close()

            # Locate recorded video
            if self.record_video:
                videos = list(video_dir.glob("*.webm"))
                if videos:
                    result.video_path = str(videos[0])
                    print(f"  🎬 Video saved: {result.video_path}")

            browser.close()

        # Save learned knowledge after every run
        self.store.save_if_dirty()
        return result

    # ── Step Dispatch ─────────────────────────────────────────────────────────

    def _execute_step(self, page: Page, step: TestStep, test_name: str) -> StepResult:
        start = time.time()
        error = None
        passed = True

        # PRE screenshot
        pre_shot = self._take_screenshot(page, step.line_number, "before")

        # DOM snapshot (for regression learning)
        dom_before = capture_dom_snapshot(page)

        try:
            action = step.action
            if   action == "navigate": self._do_navigate(page, step)
            elif action == "click":    self._do_click(page, step)
            elif action == "fill":     self._do_fill(page, step)
            elif action == "wait":     self._do_wait(page, step)
            elif action == "assert":   self._do_assert(page, step)
            elif action == "scroll":   self._do_scroll(page, step)
            elif action == "hover":    self._do_hover(page, step)
            elif action == "unknown":
                print(f"  ℹ  Skipping unrecognized step: {step.raw!r}")
            else:
                raise ValueError(f"Unknown action: {action!r}")

        except Exception as e:
            passed = False
            error = str(e)

        # POST screenshot
        post_shot = self._take_screenshot(
            page, step.line_number, "after" if passed else "FAIL"
        )

        # DOM snapshot after action
        dom_after = capture_dom_snapshot(page)

        # Save snapshot to knowledge store
        step_label = f"step_{step.line_number:02d}_{step.action}"
        self.store.save_dom_snapshot(self.run_id, step_label, dom_after)

        # Self-learning: compare with previous run's snapshot
        prev = self.store.get_last_snapshot_for_step(step_label)
        if prev and prev.get("recorded_at", "") < dom_after.get("url", ""):
            changes = self.store.compare_snapshot(dom_after, prev)
            if changes:
                print(f"    📚 DOM changes detected vs last run:")
                for c in changes[:5]:
                    print(f"       • {c}")

        duration_ms = (time.time() - start) * 1000

        return StepResult(
            step=step, passed=passed, error=error,
            screenshot=post_shot, pre_screenshot=pre_shot,
            duration_ms=duration_ms, dom_snapshot=dom_after,
        )

    def _take_screenshot(self, page: Page, line_num: int, label: str) -> Optional[str]:
        try:
            path = self.shot_dir / f"step{line_num:02d}_{label}.png"
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as e:
            print(f"    ⚠  Screenshot failed ({label}): {e!s:.50}")
            return None

    # ── Action Implementations ─────────────────────────────────────────────────

    def _do_navigate(self, page: Page, step: TestStep):
        url = step.value or ""
        if not url.startswith("http"):
            url = "https://" + url
        self._compose_ready = False
        page.goto(url, wait_until="domcontentloaded")
        self._escape_sso_if_needed(page, url)

    def _do_click(self, page: Page, step: TestStep):
        target = step.target or ""
        origin_url = page.url

        # Reset compose cache when opening a new compose window
        if any(kw in target.lower() for kw in ["new mail", "compose", "new message"]):
            self._compose_ready = False

        # SSO-guard only on landing pages (not on accounts.zoho.* etc.)
        on_own_auth = any(d in origin_url.lower() for d in ["accounts.zoho", "accounts.google", "login.microsoft"])
        on_sso_landing = not on_own_auth and any(
            kw in target.lower() for kw in ["sign in", "signin", "log in", "login"]
        )

        if on_sso_landing:
            loc = self._find_native_signin(page, target)
        else:
            loc = None

        if loc is None:
            loc = self.resolver.find_button(page, target)

        if loc is None:
            raise RuntimeError(f"Cannot find clickable element: {target!r}")

        loc.scroll_into_view_if_needed()
        try:
            loc.click(timeout=8000)
        except Exception as e:
            print(f"    ⚠  Click failed ({e!s:.60}), retrying force=True")
            loc.click(force=True)

        self._wait_for_post_click_transition(page, target)
        self._escape_sso_if_needed(page, origin_url)

    def _do_fill(self, page: Page, step: TestStep):
        target = step.target or ""
        value  = step.value or ""

        if target.lower() in ("body", "message body", "message"):
            self._fill_rich_text(page, value)
            return

        if target.lower() in ("to", "to email", "to email field", "recipient"):
            self._fill_recipient_field(page, value)
            return

        self._wait_for_field_visible(page, target)
        loc = self.resolver.find_input(page, target)
        if loc is None:
            raise RuntimeError(f"Cannot find input field: {target!r}")

        loc.scroll_into_view_if_needed()
        try:
            loc.click(timeout=5000)
        except Exception:
            try:
                loc.click(force=True, timeout=5000)
            except Exception:
                page.evaluate("el => el.focus()", loc.element_handle())

        try:
            loc.fill(value)
        except Exception:
            print(f"    ⚠  fill() failed — using keyboard")
            loc.click(force=True)
            page.keyboard.press("Control+a")
            page.keyboard.type(value)

    def _fill_recipient_field(self, page: Page, email: str):
        """Token-input aware fill for To/recipient fields."""
        print(f"    ↳ Filling recipient field with: {email!r}")

        # Wait for compose window
        self._wait_for_field_visible(page, "to")

        loc = self.resolver.find_input(page, "to")
        if loc is None:
            raise RuntimeError("Cannot find To/recipient input field")

        loc.scroll_into_view_if_needed()
        try:
            loc.click(timeout=5000)
        except Exception:
            loc.click(force=True)

        try:
            loc.fill("")
        except Exception:
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")

        # Type with delay so autocomplete fires
        page.keyboard.type(email, delay=50)
        print(f"    ↳ Typed email, waiting for autocomplete…")

        # Try autocomplete selectors sequentially (proven working)
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
                suggestion = page.locator(dropdown_sel).first
                if suggestion.is_visible(timeout=500):
                    suggestion.click()
                    print(f"    ✓ Clicked autocomplete suggestion")
                    page.wait_for_timeout(300)
                    return
                break
            except Exception:
                pass

        # Fallback: commit with Enter
        print(f"    ↳ Pressing Enter to commit recipient token")
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

    def _fill_rich_text(self, page: Page, value: str):
        """Fill iframe or contenteditable body editors."""
        for frame in page.frames:
            try:
                body = frame.locator("body[contenteditable], body")
                if body.is_visible(timeout=1000):
                    body.click()
                    body.fill(value)
                    return
            except Exception:
                pass
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

    def _wait_for_field_visible(self, page: Page, field_name: str):
        """Wait for a field to be visible. Compose-window detection is cached."""
        key = field_name.lower().strip()

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

        wait_selectors = []
        selectors = self.store.field_selectors.get(key, [])
        wait_selectors.extend(selectors[:4])

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

    def _wait_for_post_click_transition(self, page: Page, clicked_target: str):
        target_lower = clicked_target.lower()
        is_next   = any(kw in target_lower for kw in ["next", "continue", "proceed", "submit"])
        is_signin = any(kw in target_lower for kw in ["sign in", "signin", "login", "log in"])

        if is_next:
            print(f"    ↳ 'Next' clicked — waiting for password field…")
            try:
                page.wait_for_selector(
                    'input[type="password"], input[placeholder*="password" i], '
                    'input[placeholder*="Enter password" i]',
                    state="visible", timeout=8000,
                )
                print("    ✓ Password field appeared")
                return
            except Exception:
                pass

        if is_signin:
            print(f"    ↳ Sign-in clicked — waiting for page transition…")
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
                return
            except Exception:
                pass

        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(400)

    def _do_wait(self, page: Page, step: TestStep):
        condition = step.condition or ""
        try:
            page.wait_for_selector(f"text={condition}", timeout=self.timeout)
        except Exception:
            page.wait_for_function(
                f"document.body.innerText.toLowerCase().includes({condition.lower()!r})",
                timeout=self.timeout,
            )

    def _do_assert(self, page: Page, step: TestStep):
        condition = step.condition or ""
        content = page.content().lower()
        if condition.lower() not in content:
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
            loc = self.resolver.find_button(page, target)
            if loc:
                loc.scroll_into_view_if_needed()

    def _do_hover(self, page: Page, step: TestStep):
        target = step.target or ""
        loc = self.resolver.find_button(page, target)
        if loc is None:
            raise RuntimeError(f"Cannot find hover target: {target!r}")
        loc.hover()

    # ── SSO helpers ───────────────────────────────────────────────────────────

    def _is_sso_trapped(self, page: Page) -> bool:
        current = page.url.lower()
        return any(trap in current for trap in self.store.sso_trap_domains)

    def _escape_sso_if_needed(self, page: Page, origin_url: str):
        if not self._is_sso_trapped(page):
            return
        print(f"  ⚠  SSO redirect detected → {page.url}")
        for key, direct_url in self.store.direct_login_urls.items():
            if key in origin_url.lower():
                print(f"  ↩  Navigating to direct login: {direct_url}")
                page.goto(direct_url, wait_until="domcontentloaded")
                return
        print("  ↩  No direct URL found — pressing Back")
        page.go_back(wait_until="domcontentloaded")

    def _find_native_signin(self, page: Page, target_text: str) -> Optional[Locator]:
        from rapidfuzz import fuzz
        for sel in self.store.native_signin_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    print(f"    ↳ native sign-in via selector: {sel}")
                    return loc
            except Exception:
                pass

        try:
            current_domain = _extract_domain(page.url)
            candidates = page.locator("a, button, [role='button'], [role='link']").all()
            scored = []
            for c in candidates:
                if not _safe_visible(c):
                    continue
                text = _safe_text(c)
                href = _safe_attr(c, "href")
                if not text:
                    continue
                text_ratio = fuzz.ratio(target_text.lower(), text.lower())
                if text_ratio < 50:
                    continue
                length_penalty = max(0, len(text) - len(target_text)) * 3.0
                sso_penalty = 80 if any(t in href.lower() for t in self.store.sso_trap_domains) else 0
                domain_bonus = 25 if "accounts.zoho" in href else (10 if current_domain and current_domain in href else 0)
                final = text_ratio - length_penalty - sso_penalty + domain_bonus
                scored.append((final, c, text, href))

            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                best_final, best_loc, best_text, best_href = scored[0]
                print(f"    ↳ native sign-in WINNER: {best_text!r} (final={best_final:.1f})")
                if best_final > 40:
                    return best_loc
        except Exception as e:
            print(f"    ⚠  _find_native_signin error: {e}")
        return None
