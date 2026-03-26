"""
HTML Report Generator
Produces a self-contained test report with inline before/after screenshots for every step.
"""

import os
import base64
from typing import Optional
from datetime import datetime
from core.executor import TestResult, StepResult


def _img_b64(path: Optional[str]) -> str:
    """Read an image file and return a base64 data-URI, or empty string."""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return f"data:image/png;base64,{data}"
    except Exception:
        return ""


def generate_html_report(results: list, output_path: str = "reports/report.html"):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    total_steps   = sum(r.total for r in results)
    total_failed  = sum(r.failed_count for r in results)
    total_passed  = total_steps - total_failed
    all_passed    = total_failed == 0
    now           = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_dur     = sum(r.duration_ms for r in results) / 1000
    status_color  = "#22c55e" if all_passed else "#ef4444"
    status_text   = "ALL TESTS PASSED" if all_passed else f"{total_failed} TEST(S) FAILED"

    steps_html = ""
    for result in results:
        steps_html += f"""
        <div class="test-suite">
          <div class="suite-header">
            <span class="suite-name">📄 {result.test_file}</span>
            <span class="suite-badge {'pass' if result.passed else 'fail'}">
              {'PASS' if result.passed else 'FAIL'}
            </span>
            <span class="suite-meta">
              {result.total} steps &nbsp;·&nbsp; {result.duration_ms/1000:.2f}s &nbsp;·&nbsp; {result.browser}
            </span>
          </div>
        """

        for i, s in enumerate(result.steps, 1):
            pre_uri   = _img_b64(s.pre_screenshot)
            post_uri  = _img_b64(s.screenshot)
            row_cls   = "row-pass" if s.passed else "row-fail"
            badge_cls = "pass" if s.passed else "fail"
            badge_sym = "✓" if s.passed else "✗"
            err_html  = f'<div class="err-msg">{_esc(s.error or "")}</div>' if s.error else ""
            val_txt   = _esc((s.step.value or s.step.condition or "")[:60])
            target_chip = f'<span class="chip">target: {_esc(s.step.target)}</span>' if s.step.target else ""
            val_chip    = f'<span class="chip val">{val_txt}</span>' if val_txt else ""
            shots_html  = _screenshot_cell(i, pre_uri, post_uri, s.passed)

            steps_html += f"""
          <div class="step-row {row_cls}" id="step-{i}">
            <div class="step-meta">
              <span class="step-num">{i}</span>
              <span class="badge {badge_cls}">{badge_sym}</span>
              <div class="step-info">
                <div class="step-raw">{_esc(s.step.raw)}</div>
                <div class="step-parsed"><code>{s.step.action}</code>{target_chip}{val_chip}</div>
                {err_html}
              </div>
              <div class="step-dur">{s.duration_ms:.0f}ms</div>
            </div>
            {shots_html}
          </div>
            """

        steps_html += "</div>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Test Report — {now}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Sora:wght@300;500;700&display=swap');
  :root {{
    --bg:#0c0e16; --card:#13161f; --card2:#1a1d2a; --border:#252836;
    --text:#e2e8f0; --muted:#5a6380;
    --pass:#22c55e; --fail:#ef4444; --accent:#6366f1; --accent2:#a78bfa;
    --warn:#f59e0b; --before:#3b82f6; --after:#10b981;
  }}
  *{{ box-sizing:border-box; margin:0; padding:0; }}
  body{{ font-family:'Sora',sans-serif; background:var(--bg); color:var(--text); font-size:13px; }}

  .hero{{ background:linear-gradient(135deg,#13161f,#0c0e16); border-bottom:1px solid var(--border);
          padding:36px 48px 28px; display:flex; gap:24px; align-items:flex-start; }}
  .hero-icon{{ font-size:44px; filter:drop-shadow(0 0 18px var(--accent)); }}
  .hero-info{{ flex:1; }}
  .hero-title{{ font-size:26px; font-weight:700; letter-spacing:-0.5px; }}
  .hero-sub{{ color:var(--muted); font-size:12px; margin-top:4px; }}
  .hero-status{{ margin-top:10px; font-size:13px; font-weight:700; letter-spacing:1px;
                 color:{status_color}; display:flex; align-items:center; gap:8px; }}
  .hero-status::before{{ content:''; width:9px; height:9px; border-radius:50%;
                          background:{status_color}; box-shadow:0 0 8px {status_color}; }}

  .stats-bar{{ display:flex; gap:1px; background:var(--border); border-bottom:1px solid var(--border); }}
  .stat-card{{ flex:1; background:var(--card); padding:18px 24px; text-align:center; }}
  .stat-num{{ font-size:32px; font-weight:700; font-family:'JetBrains Mono',monospace; }}
  .stat-label{{ font-size:10px; text-transform:uppercase; letter-spacing:1.5px; color:var(--muted); margin-top:4px; }}
  .green{{color:var(--pass);}} .red{{color:var(--fail);}} .blue{{color:var(--accent2);}} .amber{{color:var(--warn);}}

  .content{{ padding:28px 40px; max-width:1500px; }}

  .test-suite{{ background:var(--card); border:1px solid var(--border); border-radius:12px;
                margin-bottom:24px; overflow:hidden; }}
  .suite-header{{ display:flex; align-items:center; gap:12px; padding:14px 18px;
                  background:rgba(255,255,255,.025); border-bottom:1px solid var(--border); }}
  .suite-name{{ flex:1; font-weight:600; font-size:13px; }}
  .suite-meta{{ font-size:11px; color:var(--muted); }}
  .suite-badge{{ font-size:11px; font-weight:700; border-radius:6px; padding:3px 12px; }}
  .suite-badge.pass{{ background:rgba(34,197,94,.15); color:var(--pass); }}
  .suite-badge.fail{{ background:rgba(239,68,68,.15);  color:var(--fail); }}

  .step-row{{ border-bottom:1px solid rgba(255,255,255,.04); transition:background .15s; }}
  .step-row:last-child{{ border-bottom:none; }}
  .step-row:hover{{ background:rgba(255,255,255,.02); }}
  .row-fail{{ background:rgba(239,68,68,.05); }}

  .step-meta{{ display:flex; align-items:flex-start; gap:12px; padding:12px 16px 10px; }}
  .step-num{{ width:24px; height:24px; border-radius:50%; background:var(--card2); color:var(--muted);
              font-size:11px; font-weight:700; display:flex; align-items:center; justify-content:center;
              flex-shrink:0; margin-top:1px; }}
  .step-info{{ flex:1; }}
  .step-raw{{ color:var(--text); line-height:1.5; margin-bottom:4px; }}
  .step-parsed{{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
  .step-dur{{ font-size:11px; color:var(--muted); white-space:nowrap; padding-top:2px; }}

  .badge{{ display:inline-flex; align-items:center; justify-content:center;
           font-size:11px; font-weight:700; border-radius:50%; width:22px; height:22px; flex-shrink:0; }}
  .badge.pass{{ background:rgba(34,197,94,.15); color:var(--pass); }}
  .badge.fail{{ background:rgba(239,68,68,.15);  color:var(--fail); }}
  code{{ font-family:'JetBrains Mono',monospace; font-size:11px;
         background:rgba(255,255,255,.07); padding:2px 7px; border-radius:4px; color:var(--accent2); }}
  .chip{{ font-size:11px; background:rgba(255,255,255,.05); padding:2px 8px; border-radius:4px;
          color:#94a3b8; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .chip.val{{ color:#67e8f9; }}
  .err-msg{{ margin-top:5px; color:var(--fail); font-size:11px;
             font-family:'JetBrains Mono',monospace; background:rgba(239,68,68,.08);
             padding:4px 8px; border-radius:4px; border-left:3px solid var(--fail); word-break:break-word; }}

  /* ── Screenshot strip ── */
  .shot-strip{{ display:flex; border-top:1px solid var(--border); background:var(--bg); }}
  .shot-panel{{ flex:1; min-width:0; display:flex; flex-direction:column;
                border-right:1px solid var(--border); }}
  .shot-panel:last-child{{ border-right:none; }}
  .shot-label{{ padding:6px 12px; font-size:10px; font-weight:600;
                letter-spacing:1px; text-transform:uppercase;
                display:flex; align-items:center; gap:6px; }}
  .shot-label.before{{ background:rgba(59,130,246,.12); color:var(--before);
                        border-bottom:1px solid rgba(59,130,246,.2); }}
  .shot-label.after{{ background:rgba(16,185,129,.12); color:var(--after);
                       border-bottom:1px solid rgba(16,185,129,.2); }}
  .shot-label.fail{{ background:rgba(239,68,68,.12); color:var(--fail);
                      border-bottom:1px solid rgba(239,68,68,.2); }}
  .shot-label.empty{{ background:rgba(255,255,255,.03); color:var(--muted);
                       border-bottom:1px solid var(--border); }}
  .shot-img{{ width:100%; cursor:zoom-in; display:block; transition:opacity .15s; }}
  .shot-img:hover{{ opacity:.85; }}
  .shot-placeholder{{ height:80px; display:flex; align-items:center;
                       justify-content:center; color:var(--muted); font-size:11px; }}

  /* ── Lightbox ── */
  .lightbox{{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.92);
              z-index:9999; align-items:center; justify-content:center; cursor:zoom-out; }}
  .lightbox.active{{ display:flex; }}
  .lightbox img{{ max-width:94vw; max-height:94vh; border-radius:8px;
                  box-shadow:0 0 60px rgba(0,0,0,.8); }}
  .lb-close{{ position:fixed; top:20px; right:28px; color:#fff; font-size:32px;
              cursor:pointer; opacity:.7; font-weight:300; line-height:1; }}
  .lb-close:hover{{ opacity:1; }}
  .lb-label{{ position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
              color:#fff; font-size:12px; opacity:.5;
              font-family:'JetBrains Mono',monospace; }}

  .footer{{ text-align:center; color:var(--muted); font-size:11px; margin:36px 0 12px; }}
  ::-webkit-scrollbar{{ width:6px; height:6px; }}
  ::-webkit-scrollbar-track{{ background:transparent; }}
  ::-webkit-scrollbar-thumb{{ background:var(--border); border-radius:3px; }}
</style>
</head>
<body>

<div class="lightbox" id="lightbox" onclick="closeLB()">
  <span class="lb-close" onclick="closeLB()">×</span>
  <img id="lb-img" src="" alt="screenshot"/>
  <div class="lb-label" id="lb-label"></div>
</div>

<div class="hero">
  <div class="hero-icon">🎭</div>
  <div class="hero-info">
    <div class="hero-title">Playwright NLP Test Report</div>
    <div class="hero-sub">Generated: {now} &nbsp;·&nbsp; Screenshots captured for every step</div>
    <div class="hero-status">{status_text}</div>
  </div>
</div>

<div class="stats-bar">
  <div class="stat-card"><div class="stat-num blue">{len(results)}</div><div class="stat-label">Test Files</div></div>
  <div class="stat-card"><div class="stat-num" style="color:var(--text)">{total_steps}</div><div class="stat-label">Total Steps</div></div>
  <div class="stat-card"><div class="stat-num green">{total_passed}</div><div class="stat-label">Passed</div></div>
  <div class="stat-card"><div class="stat-num red">{total_failed}</div><div class="stat-label">Failed</div></div>
  <div class="stat-card"><div class="stat-num amber">{total_dur:.1f}s</div><div class="stat-label">Duration</div></div>
</div>

<div class="content">
{steps_html}
  <div class="footer">🎭 Playwright-NLP Offline Testing Framework &nbsp;·&nbsp; {now}</div>
</div>

<script>
function openLB(src, label) {{
  document.getElementById('lb-img').src = src;
  document.getElementById('lb-label').textContent = label;
  document.getElementById('lightbox').classList.add('active');
}}
function closeLB() {{
  document.getElementById('lightbox').classList.remove('active');
  document.getElementById('lb-img').src = '';
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeLB(); }});
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


def _screenshot_cell(step_num: int, pre_uri: str, post_uri: str, passed: bool) -> str:
    def panel(uri: str, label_text: str, cls: str) -> str:
        if uri:
            return f"""<div class="shot-panel">
          <div class="shot-label {cls}">{label_text}</div>
          <img class="shot-img" src="{uri}" alt="{label_text}"
               onclick="openLB(this.src,'Step {step_num} — {label_text}')" loading="lazy"/>
        </div>"""
        return f"""<div class="shot-panel">
          <div class="shot-label empty">{label_text}</div>
          <div class="shot-placeholder">no screenshot</div>
        </div>"""

    after_cls   = "after" if passed else "fail"
    after_label = "✓ After" if passed else "✗ After (FAIL)"
    return (f'<div class="shot-strip">'
            f'{panel(pre_uri, "● Before", "before")}'
            f'{panel(post_uri, after_label, after_cls)}'
            f'</div>')


def _esc(text: str) -> str:
    return (text.replace("&","&amp;").replace("<","&lt;")
                .replace(">","&gt;").replace('"',"&quot;"))
