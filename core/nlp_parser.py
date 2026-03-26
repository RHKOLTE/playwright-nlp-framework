"""
Offline NLP Parser for Plain-English Test Scripts
Parses natural language test steps into structured actions.
No external API or model download required.
"""

import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from rapidfuzz import process, fuzz


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class TestStep:
    raw: str
    action: str                    # navigate | click | fill | wait | assert | scroll | hover
    target: Optional[str] = None   # selector hint / label text
    value: Optional[str] = None    # text to type / URL
    condition: Optional[str] = None
    line_number: int = 0

    def __repr__(self):
        parts = [f"[{self.action.upper()}]"]
        if self.target:
            parts.append(f"target={self.target!r}")
        if self.value:
            parts.append(f"value={self.value!r}")
        return " ".join(parts)


# ─── Keyword Maps ─────────────────────────────────────────────────────────────

ACTION_PATTERNS = [
    # Navigate
    (r"(?:navigate|go)\s+to\s+(.+)", "navigate"),
    (r"open\s+(?:the\s+)?(?:url|page|site)?\s*[:\-]?\s*(https?://\S+)", "navigate"),

    # Fill / Type  ← these must come BEFORE generic click patterns
    (r"fill\s+(?:the\s+)?(.+?)\s+with\s+[\"']?(.+?)[\"']?\s*$", "fill"),
    (r"type\s+[\"']?(.+?)[\"']?\s+(?:in(?:to)?|inside)\s+(?:the\s+)?(.+)", "fill_reverse"),
    (r"enter\s+[\"']?(.+?)[\"']?\s+(?:in(?:to)?|inside|for)\s+(?:the\s+)?(.+)", "fill_reverse"),
    (r"set\s+(?:the\s+)?(.+?)\s+(?:to|as)\s+[\"']?(.+?)[\"']?\s*$", "fill"),

    # Locate + Fill  — "Locate the form element with X and then fill it with Y"
    (r'locate\s+(?:the\s+)?(?:form\s+)?(?:element|field|input)\s+with\s+["\']?(.+?)["\']?\s+and\s+(?:then\s+)?fill\s+it\s+with\s+["\']?(.+?)["\']?\s*$', "fill"),
    (r'locate\s+["\'](.+?)["\']\s+and\s+(?:then\s+)?(?:fill|type)\s+["\']?(.+?)["\']?\s*$', "fill"),
    (r'find\s+(?:the\s+)?(?:form\s+)?(?:element|field|input)\s+["\']?(.+?)["\']?\s+and\s+(?:then\s+)?fill\s+(?:it\s+)?with\s+["\']?(.+?)["\']?\s*$', "fill"),

    # Click
    (r'click\s+"(.+?)"', "click"),
    (r"click\s+(?:on\s+)?(?:the\s+)?(.+)", "click"),
    (r"press\s+(?:the\s+)?(.+)", "click"),
    (r"tap\s+(?:on\s+)?(?:the\s+)?(.+)", "click"),
    (r"select\s+(?:the\s+)?(.+)", "click"),

    # Wait / Assert
    (r"wait\s+(?:for\s+)?(?:the\s+)?(.+?)\s+(?:to\s+(?:be\s+)?(?:displayed|visible|appear|load))", "wait"),
    (r"once\s+(.+?)\s+is\s+(?:displayed|visible|loaded|shown)", "wait"),
    (r"verify\s+(?:that\s+)?(?:the\s+)?(.+)", "assert"),
    (r"assert\s+(?:that\s+)?(?:the\s+)?(.+)", "assert"),
    (r"check\s+(?:that\s+)?(?:the\s+)?(.+)", "assert"),

    # Scroll
    (r"scroll\s+(?:down|up|to)\s+(.+)", "scroll"),
    (r"scroll\s+(down|up)", "scroll"),

    # Hover
    (r"hover\s+(?:over\s+)?(?:the\s+)?(.+)", "hover"),
]

# Dynamic value substitutions
DYNAMIC_VALUES = {
    r"today\s*date\s*time": lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    r"today\s*date": lambda: datetime.now().strftime("%Y-%m-%d"),
    r"current\s*time": lambda: datetime.now().strftime("%H:%M:%S"),
    r"timestamp": lambda: str(int(datetime.now().timestamp())),
}

# Common field name normalization
FIELD_ALIASES = {
    # ── Email ──
    "email field": "email",
    "email address field": "email",
    "email address": "email",
    "email address or mobile number": "email",   # ← Zoho login placeholder
    "mobile number": "email",
    "username": "email",
    "username field": "email",
    "login field": "email",
    "user id": "email",
    # ── Password ──
    "password field": "password",
    "pass field": "password",
    "enter password": "password",
    "password input": "password",
    # ── Compose ──
    "subject field": "subject",
    "subject line": "subject",
    "body field": "body",
    "message field": "body",
    "message body": "body",
    "mail body": "body",
    "compose body": "body",
    "to field": "to",
    "to email": "to",
    "to email field": "to",
    "recipient": "to",
    "recipient field": "to",
}


# ─── Parser ────────────────────────────────────────────────────────────────────

class NLPParser:
    """
    Rule-based NLP parser. 100% offline.
    Converts plain-English test instructions → TestStep objects.
    """

    def parse_file(self, path: str) -> list[TestStep]:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return self.parse_lines(lines)

    def parse_text(self, text: str) -> list[TestStep]:
        lines = text.strip().splitlines()
        return self.parse_lines(lines)

    def parse_lines(self, lines: list[str]) -> list[TestStep]:
        steps = []
        line_num = 0
        for raw in lines:
            line_num += 1
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            step = self._parse_line(stripped, line_num)
            if step:
                steps.append(step)
        return steps

    # ── Internal ──────────────────────────────────────────────────────────────

    def _parse_line(self, line: str, line_num: int) -> Optional[TestStep]:
        # Strip leading step markers like "1." "Step 1:" "-" "•"
        line = re.sub(r"^(\d+[\.\)]\s*|step\s*\d+\s*[:\-]\s*|[-•]\s*)", "", line, flags=re.I)
        line = line.strip()
        if not line:
            return None

        for pattern, action in ACTION_PATTERNS:
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                return self._build_step(action, m, line, line_num)

        # Fallback: treat as a comment / unknown
        return TestStep(raw=line, action="unknown", line_number=line_num)

    def _build_step(self, action: str, match: re.Match, raw: str, line_num: int) -> TestStep:
        groups = [g.strip() if g else g for g in match.groups()]

        if action == "navigate":
            url = groups[0] if groups else ""
            return TestStep(raw=raw, action="navigate", value=url, line_number=line_num)

        elif action == "fill":
            target = self._normalize_field(groups[0]) if groups else ""
            value = self._resolve_dynamic(groups[1]) if len(groups) > 1 else ""
            return TestStep(raw=raw, action="fill", target=target, value=value, line_number=line_num)

        elif action == "fill_reverse":
            # "type VALUE into FIELD"
            value = self._resolve_dynamic(groups[0]) if groups else ""
            target = self._normalize_field(groups[1]) if len(groups) > 1 else ""
            return TestStep(raw=raw, action="fill", target=target, value=value, line_number=line_num)

        elif action == "click":
            target = groups[0] if groups else ""
            # Strip surrounding quotes
            target = re.sub(r'^["\']|["\']$', "", target).strip()
            return TestStep(raw=raw, action="click", target=target, line_number=line_num)

        elif action == "wait":
            condition = groups[0] if groups else ""
            return TestStep(raw=raw, action="wait", condition=condition, line_number=line_num)

        elif action == "assert":
            condition = groups[0] if groups else ""
            return TestStep(raw=raw, action="assert", condition=condition, line_number=line_num)

        elif action == "scroll":
            target = groups[0] if groups else "down"
            return TestStep(raw=raw, action="scroll", target=target, line_number=line_num)

        elif action == "hover":
            target = groups[0] if groups else ""
            return TestStep(raw=raw, action="hover", target=target, line_number=line_num)

        return TestStep(raw=raw, action="unknown", line_number=line_num)

    def _normalize_field(self, text: str) -> str:
        text = text.strip().lower()
        # Direct alias match
        if text in FIELD_ALIASES:
            return FIELD_ALIASES[text]
        # Fuzzy alias match
        best, score, _ = process.extractOne(text, FIELD_ALIASES.keys(), scorer=fuzz.ratio)
        if score >= 80:
            return FIELD_ALIASES[best]
        return text

    def _resolve_dynamic(self, value: str) -> str:
        """Replace tokens like 'Today date time' with actual runtime values."""
        for pattern, resolver in DYNAMIC_VALUES.items():
            value = re.sub(pattern, lambda _: resolver(), value, flags=re.I)
        # Remove surrounding quotes left over
        value = re.sub(r'^["\']|["\']$', "", value).strip()
        return value
