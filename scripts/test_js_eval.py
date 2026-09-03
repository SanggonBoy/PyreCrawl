"""Regression test for scrape_stealth js / wait_for params (temp-mail case, 2026-09-04).

Background: temp-mail.org writes the generated address into the DOM *property*
``input#mail.value`` after an XHR. The serialized HTML still reads ``Memuat``,
so no HTML parser can ever see the address — only a live ``page.evaluate()``
can. These params expose exactly that over MCP (a raw callable can't cross
JSON-RPC, so PyreCrawl wraps a JS *expression string* into page_action).

Run:  python scripts/test_js_eval.py
Exit 0 = PASS, exit 1 = FAIL.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyrecrawl.engines import scrape_stealth

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}  {detail if not cond else ''}")
    if not cond:
        failures.append(f"{name}: {detail}")


def main() -> int:
    print("== js: read live DOM property (document.title) ==")
    r = scrape_stealth("https://example.com", timeout=60,
                       js="document.title")
    check("status 200", r.status == 200, str(r.status))
    check("js_result == 'Example Domain'",
          r.meta.get("js_result") == "Example Domain",
          repr(r.meta.get("js_result")))
    check("no js_error", "js_error" not in r.meta, str(r.meta.get("js_error")))

    print("== wait_for: immediately-true predicate ==")
    r2 = scrape_stealth("https://example.com", timeout=60,
                        wait_for="document.readyState === 'complete'",
                        js="document.readyState")
    check("no wait_for_error", "wait_for_error" not in r2.meta,
          str(r2.meta.get("wait_for_error")))
    check("js_result == 'complete'", r2.meta.get("js_result") == "complete",
          repr(r2.meta.get("js_result")))

    print("== js error path: throwing expression is captured, not raised ==")
    r3 = scrape_stealth("https://example.com", timeout=60,
                        js="document.nonexistent.foo.bar")
    check("js_error captured", "js_error" in r3.meta, str(r3.meta))
    check("no js_result on error", r3.meta.get("js_result") is None,
          repr(r3.meta.get("js_result")))
    check("page still returned", r3.status == 200, str(r3.status))

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'}")
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
