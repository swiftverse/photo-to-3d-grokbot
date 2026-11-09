"""Smoke-test a generated viewer: load it headless, drag to orbit, save before/after screenshots.

Usage: python tools/check_viewer.py out/car/viewer.html out/car/viewer_check
"""
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_3d import find_chrome
html = Path(sys.argv[1]).resolve().as_uri()
with sync_playwright() as p:
    chrome = find_chrome(); kw = dict(executable_path=chrome) if chrome else {}
    b = p.chromium.launch(**kw, headless=True, args=["--use-angle=swiftshader","--enable-unsafe-swiftshader"])
    pg = b.new_page(viewport={"width": 1000, "height": 620})
    errs = []; pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    t0 = time.time(); pg.goto(html); pg.wait_for_function("window.__ready === true", timeout=60000)
    print("load s", round(time.time()-t0,2)); time.sleep(0.5)
    pg.screenshot(path=sys.argv[2] + "_a.png")
    pg.mouse.move(500, 300); pg.mouse.down()
    for i in range(20): pg.mouse.move(500 + i*12, 300 + i*2)
    pg.mouse.up(); time.sleep(1.0)
    pg.screenshot(path=sys.argv[2] + "_b.png")
    print("console errors:", errs or "none"); b.close()
    sys.exit(1 if errs else 0)
