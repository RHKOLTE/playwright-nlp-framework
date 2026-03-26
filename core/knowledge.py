"""
Knowledge Store — Self-Learning Selector & Site Registry
=========================================================
Replaces all hardcoded maps (FIELD_SELECTOR_MAP, PLACEHOLDER_EXACT_MAP,
ZOHO_MAIL_SELECTORS, DIRECT_LOGIN_URLS, NATIVE_SIGNIN_SELECTORS).

Data is persisted to knowledge/knowledge.json and grows automatically:
  - Every successful selector is recorded and promoted to the top
  - Failed selectors are demoted / flagged
  - New field names / button names discovered at runtime are added
  - DOM snapshots saved per step for regression comparison

Schema (knowledge.json):
{
  "version": 2,
  "field_selectors": {          # field_name → [selector, ...]  ordered best-first
    "email": [...],
    "password": [...],
    ...
  },
  "placeholder_map": {          # exact placeholder text → canonical field name
    "email address or mobile number": "email",
    ...
  },
  "button_selectors": {         # button label (lower) → [css selector, ...]
    "new mail": [...],
    "send": [...],
    ...
  },
  "sso_trap_domains": [...],    # domains that trigger SSO redirect
  "direct_login_urls": {        # site key → direct login URL
    "zoho.com/mail": "https://..."
  },
  "native_signin_selectors": [...],  # CSS selectors for native login links
  "selector_stats": {           # selector → {hits, misses, last_seen}
    "input[type='email']": {"hits": 12, "misses": 0, "last_seen": "..."}
  },
  "dom_snapshots": {            # run_id/step → lightweight DOM fingerprint
    "2024-01-01T10:00:00/step_01": { "url": "...", "inputs": [...], "buttons": [...] }
  }
}
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path


KNOWLEDGE_FILE = Path(__file__).parent.parent / "knowledge" / "knowledge.json"

# ─── Seed data (only used when knowledge.json doesn't exist yet) ──────────────
SEED = {
    "version": 2,
    "field_selectors": {
        "email": [
            'input[placeholder*="Email address" i]',
            'input[placeholder*="mobile number" i]',
            'input[type="email"]',
            'input[name*="email" i]',
            'input[placeholder*="email" i]',
            'input[id*="email" i]',
            '#login-account-email',
        ],
        "password": [
            'input[placeholder="Enter password"]',
            'input[placeholder*="Enter password" i]',
            'input[type="password"]',
            'input[name*="password" i]',
            'input[placeholder*="password" i]',
            'input[id*="password" i]',
        ],
        "username": [
            'input[name*="username" i]',
            'input[id*="username" i]',
            'input[placeholder*="username" i]',
            'input[autocomplete="username"]',
        ],
        "to": [
            'input[aria-label="To"]',
            'input[aria-label*="To" i]',
            'input[id*="ZohoMail_To" i]',
            'div[id*="compose"] input[aria-label*="To" i]',
            'div[class*="compose"] input[aria-label*="To" i]',
            'input[placeholder*="Recipients" i]',
            'input[name="to"]',
            'input[placeholder*="to" i]',
            '.zc-cnt-cmt input',
            'input[id*="to" i]',
        ],
        "subject": [
            'input[id*="subject" i]',
            'input[name*="subject" i]',
            'input[placeholder*="subject" i]',
            'input[aria-label*="subject" i]',
            'div[id*="compose"] input[type="text"]',
            'div[class*="compose"] input[type="text"]:not([aria-label*="To" i])',
            '.editor-subject input',
            '#subject',
        ],
        "body": [
            'div[contenteditable="true"]',
            'div[contenteditable="true"][aria-label*="message" i]',
            'div[contenteditable="true"][aria-label*="body" i]',
            '.mail-editor div[contenteditable]',
            '#zel_editor',
            'textarea[name*="body"]',
        ],
        "search": [
            'input[type="search"]',
            'input[placeholder*="search" i]',
            'input[aria-label*="search" i]',
            'input[name*="search" i]',
            'input[id*="search" i]',
        ],
        "phone": [
            'input[type="tel"]',
            'input[name*="phone" i]',
            'input[placeholder*="phone" i]',
            'input[id*="phone" i]',
        ],
        "name": [
            'input[name="name"]',
            'input[placeholder*="name" i]',
            'input[id*="name" i]',
            'input[autocomplete*="name"]',
        ],
        "first name": [
            'input[name*="firstname" i]', 'input[name="first_name"]',
            'input[placeholder*="first name" i]', 'input[id*="firstname" i]',
        ],
        "last name": [
            'input[name*="lastname" i]', 'input[name="last_name"]',
            'input[placeholder*="last name" i]', 'input[id*="lastname" i]',
        ],
    },
    "placeholder_map": {
        "email address or mobile number": "email",
        "email address":                  "email",
        "mobile number":                  "email",
        "enter password":                 "password",
        "password":                       "password",
        "enter email":                    "email",
        "your email":                     "email",
        "user name":                      "username",
    },
    "button_selectors": {
        # Zoho Mail
        "new mail": [
            'div[id*="compose_btn" i]',
            'button[title*="New Mail" i]',
            'span[title*="New Mail" i]',
            '[aria-label*="New Mail" i]',
            'div[class*="compose"][role="button"]',
            'td[id*="newmail" i]',
        ],
        "send": [
            'button[title*="Send" i]',
            '[aria-label*="Send" i]',
            'span[title*="Send" i]',
            'div[id*="send_btn" i]',
            'li[id*="sendbutton" i]',
        ],
        "my profile": [
            '#currentUserInfo',
            'div[id*="userinfo" i]',
            'div[class*="userinfo" i]',
            'span[id*="userDisplayName" i]',
            '[aria-label*="profile" i]',
            'img[class*="avatar" i]',
            'div[class*="avatar" i]',
        ],
        "sign out": [
            'a[href*="signout" i]',
            'a[href*="logout" i]',
            '[id*="signout" i]',
            'li[title*="Sign Out" i]',
            '[aria-label*="Sign Out" i]',
        ],
    },
    "sso_trap_domains": [
        "accounts.google.com",
        "login.microsoftonline.com",
        "appleid.apple.com",
        "www.facebook.com/login",
        "linkedin.com/oauth",
        "twitter.com/i/oauth",
    ],
    "direct_login_urls": {
        "zoho.com/mail": (
            "https://accounts.zoho.in/signin"
            "?servicename=VirtualOffice"
            "&signupurl=https://www.zoho.com/mail/signup.html"
            "&serviceurl=https://mail.zoho.in"
        ),
        "zoho.com": "https://accounts.zoho.in/signin",
    },
    "native_signin_selectors": [
        'a[href*="zoho"][href*="signin"]:not([href*="google"]):not([href*="facebook"])',
        'a[href*="accounts.zoho"]',
        'a.login-with-email',
        '[data-provider="native"]',
    ],
    "selector_stats": {},
    "dom_snapshots": {},
}


class KnowledgeStore:
    """
    Persistent, self-learning selector knowledge base.
    Loads from / saves to knowledge/knowledge.json.
    All hardcoded maps are replaced by this store.
    """

    def __init__(self):
        self._data: dict = {}
        self._dirty = False
        self.load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self):
        """Load knowledge from disk, seeding with defaults if not present."""
        KNOWLEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if KNOWLEDGE_FILE.exists():
            try:
                with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                print(f"  📚 Knowledge loaded: {KNOWLEDGE_FILE}")
                return
            except Exception as e:
                print(f"  ⚠  Knowledge file corrupt ({e}), re-seeding")
        # First run or corrupt — seed from defaults
        import copy
        self._data = copy.deepcopy(SEED)
        self.save()
        print(f"  📚 Knowledge seeded: {KNOWLEDGE_FILE}")

    def save(self):
        """Persist knowledge to disk."""
        try:
            with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            self._dirty = False
        except Exception as e:
            print(f"  ⚠  Could not save knowledge: {e}")

    def save_if_dirty(self):
        if self._dirty:
            self.save()

    # ── Getters (used by SelectorResolver) ───────────────────────────────────

    @property
    def field_selectors(self) -> dict:
        return self._data.get("field_selectors", {})

    @property
    def placeholder_map(self) -> dict:
        return self._data.get("placeholder_map", {})

    @property
    def button_selectors(self) -> dict:
        return self._data.get("button_selectors", {})

    @property
    def sso_trap_domains(self) -> list:
        return self._data.get("sso_trap_domains", [])

    @property
    def direct_login_urls(self) -> dict:
        return self._data.get("direct_login_urls", {})

    @property
    def native_signin_selectors(self) -> list:
        return self._data.get("native_signin_selectors", [])

    # ── Self-learning: record selector outcomes ───────────────────────────────

    def record_hit(self, selector: str, field_name: Optional[str] = None):
        """A selector successfully found an element — promote it."""
        stats = self._data.setdefault("selector_stats", {})
        entry = stats.setdefault(selector, {"hits": 0, "misses": 0, "last_seen": ""})
        entry["hits"] += 1
        entry["last_seen"] = _now()

        # Promote selector to top of its field list
        if field_name:
            selectors = self._data["field_selectors"].get(field_name, [])
            if selector in selectors and selectors.index(selector) > 0:
                selectors.remove(selector)
                selectors.insert(0, selector)
                self._data["field_selectors"][field_name] = selectors

        self._dirty = True

    def record_miss(self, selector: str):
        """A selector failed to find an element — track for later cleanup."""
        stats = self._data.setdefault("selector_stats", {})
        entry = stats.setdefault(selector, {"hits": 0, "misses": 0, "last_seen": ""})
        entry["misses"] += 1
        self._dirty = True

    def learn_field_selector(self, field_name: str, selector: str, promote: bool = True):
        """
        Add a newly discovered selector for a field.
        Called when the fuzzy-scan fallback finds an element not in the map.
        """
        field_name = field_name.lower().strip()
        selectors = self._data["field_selectors"].setdefault(field_name, [])
        if selector not in selectors:
            if promote:
                selectors.insert(0, selector)
            else:
                selectors.append(selector)
            print(f"  📚 Learned new selector for '{field_name}': {selector}")
            self._dirty = True

    def learn_button_selector(self, label: str, selector: str):
        """Add a newly discovered selector for a button label."""
        label = label.lower().strip()
        selectors = self._data["button_selectors"].setdefault(label, [])
        if selector not in selectors:
            selectors.insert(0, selector)
            print(f"  📚 Learned new button selector for '{label}': {selector}")
            self._dirty = True

    def learn_placeholder(self, placeholder_text: str, field_name: str):
        """Map a new placeholder string to a canonical field name."""
        key = placeholder_text.lower().strip()
        if key not in self._data["placeholder_map"]:
            self._data["placeholder_map"][key] = field_name
            print(f"  📚 Learned placeholder mapping: '{key}' → '{field_name}'")
            self._dirty = True

    def learn_direct_login(self, site_key: str, url: str):
        """Record a direct login URL for a site."""
        if site_key not in self._data["direct_login_urls"]:
            self._data["direct_login_urls"][site_key] = url
            print(f"  📚 Learned direct login URL for '{site_key}'")
            self._dirty = True

    # ── DOM Snapshot (regression baseline) ───────────────────────────────────

    def save_dom_snapshot(self, run_id: str, step_label: str, snapshot: dict):
        """
        Save a lightweight DOM fingerprint for a step.
        Used to detect UI changes between runs.
        """
        key = f"{run_id}/{step_label}"
        self._data.setdefault("dom_snapshots", {})[key] = {
            **snapshot,
            "recorded_at": _now(),
        }
        self._dirty = True

    def get_dom_snapshot(self, run_id: str, step_label: str) -> Optional[dict]:
        key = f"{run_id}/{step_label}"
        return self._data.get("dom_snapshots", {}).get(key)

    def get_last_snapshot_for_step(self, step_label: str) -> Optional[dict]:
        """Find the most recent snapshot for a given step label across all runs."""
        all_snaps = self._data.get("dom_snapshots", {})
        matches = {k: v for k, v in all_snaps.items() if k.endswith(f"/{step_label}")}
        if not matches:
            return None
        latest_key = max(matches, key=lambda k: matches[k].get("recorded_at", ""))
        return matches[latest_key]

    def compare_snapshot(self, current: dict, previous: dict) -> list[str]:
        """
        Compare two DOM snapshots and return a list of change descriptions.
        Used for self-learning: if selectors changed, update the knowledge.
        """
        changes = []

        prev_inputs = {i["label"]: i for i in previous.get("inputs", [])}
        curr_inputs = {i["label"]: i for i in current.get("inputs", [])}

        for label, prev in prev_inputs.items():
            if label not in curr_inputs:
                changes.append(f"Input '{label}' disappeared")
            else:
                curr = curr_inputs[label]
                for attr in ["type", "id", "name", "placeholder", "aria_label"]:
                    if prev.get(attr) != curr.get(attr):
                        changes.append(
                            f"Input '{label}' attr '{attr}' changed: "
                            f"{prev.get(attr)!r} → {curr.get(attr)!r}"
                        )

        for label in curr_inputs:
            if label not in prev_inputs:
                changes.append(f"New input appeared: '{label}'")

        prev_buttons = set(previous.get("buttons", []))
        curr_buttons = set(current.get("buttons", []))
        for b in prev_buttons - curr_buttons:
            changes.append(f"Button '{b}' disappeared")
        for b in curr_buttons - prev_buttons:
            changes.append(f"New button appeared: '{b}'")

        return changes

    # ── Export for debugging ──────────────────────────────────────────────────

    def export_stats(self) -> dict:
        """Return selector stats sorted by hit rate."""
        stats = self._data.get("selector_stats", {})
        return dict(sorted(stats.items(),
                           key=lambda x: x[1].get("hits", 0), reverse=True))


# ── Singleton instance (shared across the whole framework) ────────────────────
_store: Optional[KnowledgeStore] = None

def get_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
