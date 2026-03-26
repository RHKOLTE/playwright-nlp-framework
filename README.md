# 🎭 Playwright-NLP Offline Test Framework

A **100% offline** plain-English test automation framework.  
Write tests in plain English → Framework parses them → Playwright executes → HTML report generated.

---

## 📁 Project Structure

```
playwright-nlp-framework/
├── run_tests.py              # ← Main entry point (CLI)
├── requirements.txt
├── core/
│   ├── nlp_parser.py         # Plain-English → TestStep objects (offline NLP)
│   ├── executor.py           # TestStep → Playwright actions + smart selectors
│   └── reporter.py           # TestResult → HTML report
├── tests/
│   └── zoho_mail.txt         # Sample test script
└── reports/
    ├── report.html           # Generated HTML report
    └── screenshots/          # Auto-captured on failures
```

---

## ⚙️ Setup (One-Time)

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Playwright browsers (offline after this)
playwright install chromium
# optional:
playwright install firefox webkit
```

> ✅ After setup, the entire framework runs **100% offline** — no API calls, no model downloads.

---

## 🚀 Running Tests

```bash
# Run a single test file
python run_tests.py tests/zoho_mail.txt

# Run all test files in a folder
python run_tests.py tests/

# Run headless (no browser window)
python run_tests.py tests/ --headless

# Run in Firefox
python run_tests.py tests/ --browser firefox

# Just parse and preview steps (no execution)
python run_tests.py tests/zoho_mail.txt --parse-only

# Custom slow-mo and timeout
python run_tests.py tests/ --slow-mo 800 --timeout 20000

# Custom report output
python run_tests.py tests/ --report reports/my_report.html
```

---

## ✍️ Writing Test Scripts

Test scripts are plain `.txt` files. Each line is one step.  
Lines starting with `#` are comments.

### Supported Actions

| Syntax | Action |
|---|---|
| `Navigate to https://...` | Open a URL |
| `Click "Button Text"` | Click any clickable element |
| `Click Sign In` | Click (without quotes too) |
| `Fill the email field with user@example.com` | Type into an input |
| `Fill the password field with "MyPass123"` | Type password |
| `Fill the body with "Hello - Today date time"` | Dynamic date/time substitution |
| `Once Inbox is displayed.` | Wait for element/text |
| `Verify that success message is shown` | Assert text is on page |
| `Scroll down` | Scroll the page |
| `Hover over My Profile` | Hover over element |

### Dynamic Values

| Token | Replaced With |
|---|---|
| `Today date time` | `2025-03-25 14:32:01` |
| `Today date` | `2025-03-25` |
| `current time` | `14:32:01` |
| `timestamp` | Unix timestamp |

### Field Name Aliases (auto-resolved)

| You write | Resolves to |
|---|---|
| `email field`, `email address field` | email input |
| `password field`, `pass field` | password input |
| `To Email`, `recipient` | To field in compose |
| `Subject`, `subject field` | Subject input |
| `Body`, `message body`, `message field` | Rich-text body |

---

## 📄 Sample Test Script

```text
# Zoho Mail - End-to-End Test

Navigate to https://www.zoho.com/mail/
Click "Sign in"
Fill the email field with user@zoho.in
Click "Next"
Fill the password field with "MyPassword123"
Click "Sign in"
Once Inbox is displayed.
Click "New Mail"
Fill the To Email with "recipient@example.com"
Fill the Subject with "Test email using playwright-nlp"
Fill the Body with "Test email - Today date time"
Click "Send"
Click "My Profile"
Click "Sign Out"
```

---

## 📊 Reports

After execution, open `reports/report.html` in any browser.

- ✅ Per-step pass/fail status
- ⏱ Timing per step
- 📸 Auto-screenshot on failures
- 🎯 Parsed action/target/value columns

---

## 🧠 How the NLP Parser Works

1. **Strips** step prefixes (`1.`, `Step 2:`, `-`, `•`)
2. **Pattern matches** against 15+ regex patterns for actions (navigate, click, fill, wait, assert, scroll, hover)
3. **Normalizes** field names via alias map + fuzzy matching (rapidfuzz)
4. **Resolves dynamic values** (`Today date time` → actual datetime)
5. Returns structured `TestStep` objects

## 🎯 How the Selector Resolver Works

For **inputs**, tries in order:
1. Known field map (CSS selectors per field type)
2. `get_by_label()` (ARIA)
3. `get_by_placeholder()`
4. `aria-label` attribute
5. `name`/`id`/`class` attribute match

For **buttons/links**, tries in order:
1. `get_by_role()` (button, link, menuitem, tab, option)
2. `get_by_text()`
3. CSS `:has-text()` selectors
4. Fuzzy match against all visible buttons/links (≥75% score)

---

## 🔧 Extending

### Add a new action

In `core/nlp_parser.py`, add a tuple to `ACTION_PATTERNS`:
```python
(r"double.?click\s+(?:on\s+)?(.+)", "double_click"),
```

Then in `core/executor.py`, add a handler in `_execute_step()`:
```python
elif action == "double_click":
    self._do_double_click(page, step)
```

### Add a new field alias

In `core/nlp_parser.py`, add to `FIELD_ALIASES`:
```python
"username field": "email",
"user field": "email",
```

### Add a new dynamic value

In `core/nlp_parser.py`, add to `DYNAMIC_VALUES`:
```python
r"random\s*number": lambda: str(random.randint(1000, 9999)),
```
