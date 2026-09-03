"""Regression test for the SPA-skeleton ladder bug (found via Shopee demo, 2026-09-04):
HTTP 200 + JS-rendered shell was accepted as success instead of escalating to stealth.

Run:  python scripts/test_ladder_bug.py
Exit 0 = PASS, exit 1 = FAIL
"""
import sys
sys.path.insert(0, r"D:\New Downloads\PyreCrawl\src")

from pyrecrawl.engines import scrape_smart, _looks_like_js_skeleton

# --- Unit level -------------------------------------------------------------
# Realistic SPA shell: big HTML payload (scripts), ~90 chars visible text.
spa_shell = (
    "<html><head><title>Shopee Indonesia</title>"
    "<script>" + ("var x=1;" * 1200) + "</script>"
    "<style>" + (".a{color:red}" * 400) + "</style>"
    "</head><body><div id='app'></div></body></html>"
)
ok1 = _looks_like_js_skeleton(spa_shell, 200) is True
print(f"[unit] SPA shell (200, big html, no text) -> skeleton={ok1}  (want True)")

# Healthy static page must NOT be flagged (avoid false-positive escalation).
static_page = (
    "<html><head><title>Docs</title></head><body>"
    + "<p>Real readable content about Python packaging and releases.</p>" * 60
    + "</body></html>"
)
ok2 = _looks_like_js_skeleton(static_page, 200) is False
print(f"[unit] static page (200, real text)        -> skeleton={not ok2}  (want False)")

# Small pages never trigger (below 5k floor).
ok3 = _looks_like_js_skeleton("<html><body>hi</body></html>", 200) is False
print(f"[unit] tiny page                           -> skeleton={not ok3}  (want False)")

# --- Integration: ladder on a known SPA ------------------------------------
SPA = "https://shopee.co.id/search?keyword=kahf+face+wash"
print(f"\n[integration] scrape_smart('{SPA[:52]}...', prefer='auto')")
try:
    r = scrape_smart(SPA, prefer="auto", timeout=30)
    print(f"       status={r.status} method={r.method} md_len={len(r.markdown or '')}")
    ok4 = r.method != "scrapling.fetch_fast"  # must have escalated
    print(f"       escalated={ok4}  (want True)")
except Exception as e:
    print(f"       EXCEPTION: {type(e).__name__}: {str(e)[:160]}")
    ok4 = False

print()
if ok1 and ok2 and ok3 and ok4:
    print("RESULT: PASS")
    sys.exit(0)
print("RESULT: FAIL")
sys.exit(1)
