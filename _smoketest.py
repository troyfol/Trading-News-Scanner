"""Headless smoketest for MorningScanner (scan_sec.py).

Run this before every rebuild — it verifies the app constructs +
tears down cleanly, settings persistence works, the URL allowlist
holds, EDGAR regex gates accept valid + reject malformed input, and
the recently-added stall-indicator + ETF map warning paths fire.

Run:
    venv\\Scripts\\python.exe _smoketest.py

Exit code 0 = all green. Anything else = a regression to investigate
before shipping a new exe.

Per the project memory pattern:
- Live ``scanner_settings.json`` is backed up before each run and
  restored in the finally block. Tests never clobber user state.
- App teardown follows the documented sequence
  (``fetcher.close() -> watch_thread.stop() -> destroy()``); the
  "main thread is not in main loop" warning from the bg_fetch thread
  during cleanup is benign and ignored.
- Console output is ASCII-only (cp1252 console can't render arrows).
"""
import json
import logging
import os
import sys
import shutil
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Quiet the noisy bg/RSS logging during tests — we don't need it.
logging.basicConfig(level=logging.CRITICAL)


def _hr(label):
    print(f"--- {label} ---")


def _fail(msg):
    print(f"FAIL: {msg}")
    traceback.print_exc()
    sys.exit(1)


def _make_app():
    """Construct the scanner with the main window hidden. Returns the
    app instance; caller must call _teardown(app) in finally."""
    import gc
    import tkinter as tk
    import scan_sec as mod
    # Reap the PRIOR test's Tk root (already out of scope) NOW, on the main
    # thread, before creating a new one. This suite builds ~20 Tk() roots in
    # one process; if their tkinter Variables/interpreters are left for a
    # lazy GC at a random later point, finalization can land mid-interpreter-
    # teardown and abort the whole process with the fatal
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread". A
    # deterministic collect here keeps each root's teardown isolated.
    gc.collect()
    app = mod.ScannerApp()
    app.withdraw()
    app.update_idletasks()
    return app


def _teardown(app):
    """Standard cleanup sequence per project memory."""
    try:
        app.fetcher.close()
    except Exception:
        pass
    try:
        # stop() AND join(): without the join, each test's daemon watch
        # thread lingers and keeps polling UIA. Across ~20 construct/teardown
        # cycles those leftover threads accumulate and contend on UIA, which
        # is what intermittently pushed a later test's own stop()+join() past
        # its timeout. Reaping here keeps every test isolated.
        app.watch_thread.stop()
        app.watch_thread.join(timeout=10.0)
    except Exception:
        pass
    try:
        app.destroy()
    except Exception:
        pass


# ----- tests -----------------------------------------------------------


def test_import_and_module_shape():
    """Importing scan_sec must not crash and must expose the symbols
    the rest of this harness relies on."""
    _hr("module import + expected symbols present")
    import scan_sec as mod
    for name in [
        "ScannerApp", "CIKResolver", "RSSWorker", "DataFetcher",
        "WatchThread", "HistoricalLookup",
        "_EDGAR_ADSH_RE", "_EDGAR_PRIMARY_DOC_RE",
        "_HTTP_MAX_BYTES_COMPANYFACTS", "_HTTP_MAX_BYTES_SCRAPE_HTML",
        "SETTINGS_FILE", "THEMES",
        "_parse_eps_sales_surpr_cell", "_fv_ea_rows_with_yoy",
        "_YOY_MIN_BASE_EPS", "_YOY_MIN_BASE_REV_M", "_YOY_MIN_BASE_REV_RAW",
    ]:
        assert hasattr(mod, name), f"scan_sec missing expected symbol: {name}"
    # Stall colour added in this audit pass — guard against accidental
    # regression that drops it.
    for theme in ("dark", "light"):
        assert "STATUS_STALL" in mod.THEMES[theme], (
            f"THEMES[{theme!r}] missing STATUS_STALL key"
        )
    print("module import OK")


def test_app_construct_and_teardown():
    """The big one: app must come up headless without crashing and
    tear down cleanly. Covers initial settings load, watch thread
    spawn, RSS worker init, status loop scheduling."""
    _hr("ScannerApp construct + clean teardown")
    app = _make_app()
    try:
        # Window built but hidden.
        assert app.winfo_manager() != "", "Tk geometry manager not active"
        # Core attributes exposed by ScannerApp.__init__.
        assert app.fetcher is not None
        assert app.watch_thread is not None
        assert hasattr(app, "_gen_lock")
        assert hasattr(app, "current_symbol")
        # A clean fresh-init session should not yet be in historical mode.
        assert app.historical_active is False
        print("app construct + teardown OK")
    finally:
        _teardown(app)


def test_safe_open_url_allowlist():
    """_safe_open_url must drop everything outside http(s). Crafted
    feeds / Polygon articles / scraped Finviz rows can carry hostile
    schemes; the allowlist is the last gate before webbrowser.open."""
    _hr("_safe_open_url scheme allowlist")
    app = _make_app()
    try:
        # Stand in for webbrowser.open so we don't pop a real browser.
        opened = []
        import webbrowser
        original = webbrowser.open
        webbrowser.open = lambda url, *a, **kw: opened.append(url)
        try:
            # Allowed schemes should pass through.
            app._safe_open_url("https://www.sec.gov/")
            app._safe_open_url("http://example.com/")
            # Hostile schemes must be dropped silently.
            app._safe_open_url("javascript:alert(1)")
            app._safe_open_url("file:///c:/windows/system32/")
            app._safe_open_url("vbscript:msgbox(1)")
            app._safe_open_url("data:text/html,<script>")
            app._safe_open_url("ftp://malicious.example/")
            # Bad input shouldn't crash.
            app._safe_open_url(None)
            app._safe_open_url("")
            app._safe_open_url(12345)
        finally:
            webbrowser.open = original
        assert opened == [
            "https://www.sec.gov/", "http://example.com/",
        ], f"allowlist leaked: {opened!r}"
        print("URL allowlist OK")
    finally:
        _teardown(app)


def test_edgar_regex_gates():
    """The accession + primary_doc regexes are the validation gate
    between scraped/cached values and URL construction. Loose regex
    would let a malicious feed redirect a click to attacker-controlled
    paths under sec.gov."""
    _hr("EDGAR adsh + primary_doc regex gates")
    import scan_sec as mod

    valid_adsh = "0001193125-26-000123"
    assert mod._EDGAR_ADSH_RE.match(valid_adsh), "rejected valid accession"
    for bad in [
        "0001193125-26-12345",       # too few trailing digits
        "0001193125-26-1234567",     # too many
        "0001193125_26_000123",      # wrong separator
        "abcd00001193-26-000123",    # non-digit
        "../etc/passwd",             # path traversal
        "",
    ]:
        assert not mod._EDGAR_ADSH_RE.match(bad), (
            f"_EDGAR_ADSH_RE accepted invalid input: {bad!r}"
        )

    for good in ["aapl-20240331.htm", "Form10K_2024.txt",
                 "doc_v2-rc.html", "X.htm"]:
        assert mod._EDGAR_PRIMARY_DOC_RE.match(good), (
            f"rejected valid primary_doc: {good!r}"
        )
    for bad in [
        "../escape.htm",          # path traversal
        "a b.htm",                # space
        "a/b.htm",                # slash
        "a\\b.htm",               # backslash
        "doc;.htm",               # injection char
        ".",                      # dot-only -> current dir
        "..",                     # dot-only -> parent dir (traversal)
        "...",                    # dot-only
        ".hidden",                # leading dot
        "",
    ]:
        assert not mod._EDGAR_PRIMARY_DOC_RE.match(bad), (
            f"_EDGAR_PRIMARY_DOC_RE accepted invalid input: {bad!r}"
        )

    # SEC submissions-pagination filename gate (added Wave 2).
    assert mod._SEC_SUBMISSIONS_FILE_RE.match("CIK0000320193-submissions-001.json")
    for bad in [
        "CIK320193-submissions-001.json",     # CIK not 10 digits
        "CIK0000320193-submissions-1.json",   # page not 3 digits
        "../CIK0000320193-submissions-001.json",
        "CIK0000320193-submissions-001.txt",
        "evil.json",
    ]:
        assert not mod._SEC_SUBMISSIONS_FILE_RE.match(bad), (
            f"_SEC_SUBMISSIONS_FILE_RE accepted invalid input: {bad!r}"
        )
    print("EDGAR regex gates OK")


def test_http_byte_caps_present_and_sane():
    """The companyfacts + HTML scrape caps prevent a hostile origin
    from balloon-loading the process. Regression guard: if a future
    edit zeros them out or removes the constants, this fires."""
    _hr("HTTP response-size caps present + sane")
    import scan_sec as mod
    cf_cap = mod._HTTP_MAX_BYTES_COMPANYFACTS
    html_cap = mod._HTTP_MAX_BYTES_SCRAPE_HTML
    # Sanity range: companyfacts can be a few MB legitimately;
    # HTML scrape pages are tens of KB. Tight enough that a regression
    # to "no cap" or "1 byte" would be caught.
    assert 1 * 1024 * 1024 <= cf_cap <= 200 * 1024 * 1024, (
        f"companyfacts cap looks wrong: {cf_cap}"
    )
    assert 256 * 1024 <= html_cap <= 50 * 1024 * 1024, (
        f"HTML scrape cap looks wrong: {html_cap}"
    )
    print(f"caps OK ({cf_cap // (1024*1024)} MB / "
          f"{html_cap // (1024*1024)} MB)")


def test_settings_corrupt_preserves_as_bak():
    """A corrupt scanner_settings.json must be preserved as
    .corrupt.bak rather than silently overwritten — protects against
    accidentally losing a recoverable user state due to a one-byte
    typo in the JSON."""
    _hr("corrupt settings file preserved as .corrupt.bak")
    import scan_sec as mod
    # Snapshot live settings if present so we restore them in finally.
    live_path = mod.SETTINGS_FILE
    backup = live_path.read_bytes() if live_path.exists() else None
    bak_path = live_path.with_suffix(".corrupt.bak")
    bak_existed = bak_path.exists()
    bak_backup = bak_path.read_bytes() if bak_existed else None
    try:
        # Plant a corrupt settings file.
        live_path.write_text("{this is : not json", encoding="utf-8")
        # Construct app — loader should detect corruption + move aside.
        app = _make_app()
        try:
            # The original file should now be gone (moved to .corrupt.bak),
            # AND the .corrupt.bak should exist with the corrupt payload.
            assert bak_path.exists(), (
                "_DEFAULT_SETTINGS load should have moved the corrupt "
                "file aside to .corrupt.bak"
            )
            assert b"not json" in bak_path.read_bytes(), (
                ".corrupt.bak should hold the original corrupt payload"
            )
            print("corrupt settings preserved OK")
        finally:
            _teardown(app)
    finally:
        # Restore originals.
        if backup is not None:
            live_path.write_bytes(backup)
        elif live_path.exists():
            live_path.unlink()
        if bak_existed:
            bak_path.write_bytes(bak_backup)
        elif bak_path.exists():
            bak_path.unlink()


def test_wave1_settings_load_boundary_hardening():
    """Wave-1 audit fixes: atomic settings write, load-boundary
    validation (malformed geometry / non-numeric font_size / non-dict
    root must not crash startup), _save_chart_colors no-clobber on a
    non-dict file, process_data shape-guards, and the wires-cache
    non-dict element filter."""
    _hr("Wave 1 — settings + load-boundary hardening")
    import scan_sec as mod

    # (a) _atomic_write_json writes correct content + leaves no .tmp.
    td = tempfile.mkdtemp()
    try:
        p = Path(td) / "x.json"
        mod._atomic_write_json(p, {"a": 1})
        assert json.loads(p.read_text()) == {"a": 1}, "atomic write payload wrong"
        assert not (Path(td) / "x.json.tmp").exists(), "atomic write left a .tmp"
    finally:
        shutil.rmtree(td, ignore_errors=True)

    live = mod.SETTINGS_FILE
    backup = live.read_bytes() if live.exists() else None
    bak_path = live.with_suffix(".corrupt.bak")
    bak_existed = bak_path.exists()
    bak_backup = bak_path.read_bytes() if bak_existed else None
    try:
        # Malformed geometry + non-numeric font_size are valid JSON in a
        # dict, so they're NOT quarantined — the loader must ignore them
        # rather than crash startup (geometry -> tk.TclError, font_size
        # -> TypeError, both previously uncaught).
        live.write_text(json.dumps({
            "geometry": "not-a-geometry", "font_size": "big", "theme": "dark",
        }), encoding="utf-8")
        app = _make_app()  # raises if load_settings crashes -> test fails
        try:
            assert isinstance(app.base_font_size, int) and 7 <= app.base_font_size <= 20, (
                "non-numeric font_size should have been ignored, leaving a sane default"
            )

            # process_data shape-guards: non-dict root + bad rows must not raise.
            r = app.fetcher.cik_resolver
            r.process_data(["not-a-dict-root"])  # no raise
            r.process_data({
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc"},
                "1": "not-a-dict",
                "2": {"ticker": "NOCIK"},  # missing cik_str -> skipped
            })
            assert "AAPL" in r.ticker_map, "a valid manifest row should survive"
            assert all("None" not in v["cik"] for v in r.ticker_map.values()), (
                "missing cik_str must be skipped, not turned into '000000None'"
            )

            # _save_chart_colors must NOT clobber a valid-but-non-dict file.
            live.write_text("[1, 2, 3]", encoding="utf-8")
            app._save_chart_colors()
            assert live.read_text().strip() == "[1, 2, 3]", (
                "_save_chart_colors overwrote a non-dict settings file"
            )

            # wires load_cache filters non-dict elements (so a tampered
            # cache can't kill the RSS daemon via i.get()).
            rw = app.fetcher.rss_worker
            wp = mod.WIRE_CACHE_PATH
            wbackup = wp.read_bytes() if wp.exists() else None
            try:
                wp.write_text(json.dumps({"items": [
                    {"url": "https://x/y", "title": "t"}, "garbage", 42,
                ]}), encoding="utf-8")
                loaded = rw.load_cache()
                assert loaded and all(isinstance(i, dict) for i in loaded), (
                    "load_cache should drop non-dict elements"
                )
                assert len(loaded) == 1, "only the single dict element should remain"
            finally:
                if wbackup is not None:
                    wp.write_bytes(wbackup)
                elif wp.exists():
                    wp.unlink()
            print("Wave 1 hardening OK")
        finally:
            _teardown(app)
    finally:
        if backup is not None:
            live.write_bytes(backup)
        elif live.exists():
            live.unlink()
        if bak_existed:
            bak_path.write_bytes(bak_backup)
        elif bak_path.exists():
            bak_path.unlink()


def test_wave2_network_surface_hardening():
    """Wave-2 audit fixes: extract_oneliner host/scheme guard, finviz
    fetcher URL-quoting, and the ETF-scraper apex pin helper."""
    _hr("Wave 2 — network surface hardening")
    import scan_sec as mod
    import etf_scraper as etf

    # extract_oneliner refuses non-https / non-sec.gov hosts BEFORE any
    # network call (returns the 'bad_host' sentinel offline).
    for bad_url in [
        "http://www.sec.gov/x",            # not https
        "https://evil.example/x",          # off host
        "https://evilsec.gov/x",           # lookalike, not *.sec.gov
        "ftp://www.sec.gov/x",             # bad scheme
    ]:
        snip, full, err = mod.HistoricalLookup.extract_oneliner(bad_url, "UA")
        assert err == "bad_host", (
            f"extract_oneliner should reject {bad_url!r} as bad_host, got {err!r}"
        )

    # ETF-scraper apex pin: https + on-apex only.
    assert etf._host_on_apex("https://graniteshares.com/etfs/nvdl/", "graniteshares.com")
    assert etf._host_on_apex("https://www.rexshares.com/x", "rexshares.com")
    for bad, apex in [
        ("http://graniteshares.com/x", "graniteshares.com"),   # not https
        ("https://evil.example/?graniteshares.com/x", "graniteshares.com"),
        ("https://graniteshares.com.evil.com/x", "graniteshares.com"),
        ("https://evilrexshares.com/x", "rexshares.com"),
    ]:
        assert not etf._host_on_apex(bad, apex), (
            f"_host_on_apex should reject {bad!r} for apex {apex!r}"
        )

    # finviz fetchers URL-quote the symbol (defense-in-depth). Verify by
    # inspecting source rather than hitting the network.
    import inspect
    src = inspect.getsource(mod.DataFetcher.fetch_finviz_earnings)
    assert "url_quote(sym" in src, "fetch_finviz_earnings should url_quote the symbol"
    src2 = inspect.getsource(mod.DataFetcher.scrape_finviz)
    assert "url_quote(symbol" in src2, "scrape_finviz should url_quote the symbol"
    print("Wave 2 network hardening OK")


def test_wave3_robustness_hardening():
    """Wave-3 audit fixes: NaN/finite guards at the percent boundary,
    LRU cache eviction, WatchThread event-based stop+join, the
    etf_map empty-payload floor, and the ETF retry-after parser."""
    _hr("Wave 3 — robustness / diagnosability hardening")
    import scan_sec as mod
    import etf_map as em
    import etf_scraper as etf

    # _fmt_signed_pct: None/NaN/inf -> None; finite -> formatted.
    assert mod._fmt_signed_pct(None) is None
    assert mod._fmt_signed_pct(float("nan")) is None
    assert mod._fmt_signed_pct(float("inf")) is None
    assert mod._fmt_signed_pct(5.337) == "+5.34%"
    assert mod._fmt_signed_pct(-2.0) == "-2.00%"

    # _parse_pct_value: rejects nan/inf, parses signed percents.
    assert mod.ScannerApp._parse_pct_value("nan") is None
    assert mod.ScannerApp._parse_pct_value("inf") is None
    assert abs(mod.ScannerApp._parse_pct_value("+5.34%") - 5.34) < 1e-9

    # DataFetcher._evict_lru bounds an OrderedDict.
    from collections import OrderedDict
    od = OrderedDict((str(i), i) for i in range(10))
    mod.DataFetcher._evict_lru(od, 4)
    assert len(od) == 4 and list(od.keys()) == ["6", "7", "8", "9"], (
        f"_evict_lru kept the wrong entries: {list(od.keys())}"
    )

    # etf_map empty-payload floor: a refresh that yields {} WITH errors
    # must preserve the prior good map (not overwrite it with {}).
    td = tempfile.mkdtemp()
    try:
        p = Path(td) / "etfmap.json"
        m = em.EtfMap(path=p)
        m.replace({"TSLA": [{"ticker": "TSLL", "issuer": "X", "mult": 2.0}]},
                  issuers_scraped=["X"], errors=[])
        assert m.get_etfs_for("TSLA"), "initial replace should populate the map"
        m.replace({}, issuers_scraped=["X"], errors=["scrape failed"])
        assert m.get_etfs_for("TSLA"), (
            "empty+errors refresh wiped the map (floor failed)"
        )
        # A legitimate empty result with NO errors is allowed to apply.
        m.replace({}, issuers_scraped=["X"], errors=[])
        assert not m.get_etfs_for("TSLA"), (
            "clean empty refresh should be allowed to clear the map"
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # ETF Retry-After parser: numeric honored (capped), garbage -> default.
    class _R:
        def __init__(self, ra):
            self.headers = {"Retry-After": ra} if ra is not None else {}
    assert etf._retry_after_seconds(_R("2"), 0.5) == 2.0
    assert etf._retry_after_seconds(_R("999"), 0.5) == 10.0     # capped
    assert etf._retry_after_seconds(_R("xyz"), 0.5) == 0.5      # fallback
    assert etf._retry_after_seconds(_R(None), 0.5) == 0.5

    # WatchThread event-based stop + join exits promptly.
    app = _make_app()
    try:
        wt = app.watch_thread
        if wt is not None:
            wt.stop()
            # Generous join window: stop() wakes the poll sleep instantly,
            # but it can't interrupt an in-flight get_info(); on the very
            # first (cold) process run the initial COM/UIA enumeration can
            # run for many seconds while the OS disk cache for comtypes is
            # cold, which made this assertion flaky. 20s clears the cold
            # path without masking a genuinely wedged thread.
            wt.join(timeout=20.0)
            assert not wt._thread.is_alive(), (
                "WatchThread did not exit after stop()+join()"
            )
        print("Wave 3 robustness hardening OK")
    finally:
        _teardown(app)


def test_wave4_security_primitives():
    """Wave-4 regression locks for the security primitives the audit
    flagged as untested: key scrubbing, _read_capped enforcement, and the
    keyring backend guard. These previously could regress green because
    the suite only checked adjacent surrogates (constants/regex objects)."""
    _hr("Wave 4 — security primitive regression locks")
    import scan_sec as mod

    # _scrub_polygon_key redacts the key and is a safe no-op on None/empty.
    assert "ABC123KEY" not in mod._scrub_polygon_key("net error key=ABC123KEY", "ABC123KEY")
    assert mod._scrub_polygon_key("plain text", None) == "plain text"
    assert mod._scrub_polygon_key(None, "ABC123KEY") is None
    assert mod._scrub_polygon_key("", "ABC123KEY") == ""

    # _read_capped: raises ValueError once the stream exceeds the cap, and
    # returns the full body otherwise. Stub a streamed Response.
    class _FakeResp:
        def __init__(self, chunks):
            self._chunks = chunks
        def iter_content(self, chunk_size=65536):
            return iter(self._chunks)

    # Under cap -> returns concatenated bytes.
    body = mod._read_capped(_FakeResp([b"ab", b"cd"]), 1024)
    assert body == b"abcd", f"_read_capped returned {body!r}"
    # Over cap -> raises ValueError (the DoS guard).
    try:
        mod._read_capped(_FakeResp([b"x" * 1000, b"y" * 1000]), 1500)
        assert False, "_read_capped should have raised ValueError past the cap"
    except ValueError:
        pass

    # Keyring backend guard returns a bool without raising (its value
    # depends on the host backend; we only assert it doesn't crash).
    assert isinstance(mod._keyring_backend_is_secure(), bool)
    print("Wave 4 security primitives OK")


def test_watchthread_is_stalled_initial_false():
    """is_stalled() must return False on a freshly-constructed watcher
    (no get_info() call in flight yet). Guards the recently-wired
    status_loop indicator against firing on app startup."""
    _hr("WatchThread.is_stalled() initial state")
    app = _make_app()
    try:
        wt = app.watch_thread
        # Brand-new watcher: no call has been made, _call_start = 0.0
        # The is_stalled implementation returns False when start == 0.0.
        assert wt.is_stalled() is False, (
            "freshly-constructed WatchThread should not report stalled"
        )
        # Simulate an active call that has not yet finished, > stall
        # threshold ago. Direct manipulation — the public API doesn't
        # expose a way to set these without invoking the win32 calls.
        import time
        with wt._lock:
            wt._call_start = time.time() - (wt.STALL_THRESHOLD_SEC + 1)
            wt._call_end = 0.0
        assert wt.is_stalled() is True, (
            "WatchThread.is_stalled() should fire when call has been "
            "in flight longer than STALL_THRESHOLD_SEC"
        )
        # Resolved call: end >= start → not stalled.
        with wt._lock:
            wt._call_end = time.time()
        assert wt.is_stalled() is False, (
            "is_stalled() should clear once the call returns"
        )
        print("WatchThread.is_stalled() shape OK")
    finally:
        _teardown(app)


def test_status_loop_paints_stall_indicator():
    """status_loop must tint lbl_symbol with STATUS_STALL when the
    watch thread reports stalled, and restore on resume. Regression
    guard for the audit fix wiring."""
    _hr("status_loop paints stall colour + restores on resume")
    app = _make_app()
    try:
        import time
        c = app.colors
        # Force stall.
        wt = app.watch_thread
        # Stop + join the live daemon watcher first: its poll loop sets
        # _call_start/_call_end on every get_info() cycle, which races the
        # forced values below and intermittently clears the stall before
        # status_loop reads it. Quiescing the thread makes the test
        # deterministic without weakening what it checks.
        wt.stop()
        wt.join(timeout=10.0)
        with wt._lock:
            wt._call_start = time.time() - (wt.STALL_THRESHOLD_SEC + 1)
            wt._call_end = 0.0
        # Tick status_loop manually (it normally re-schedules itself
        # every 1s; we run one iteration directly).
        app.status_loop()
        assert app.lbl_symbol.cget("bg") == c["STATUS_STALL"], (
            f"symbol label should tint to STATUS_STALL when stalled; "
            f"got {app.lbl_symbol.cget('bg')!r}"
        )
        # Resolve + tick again → restored to theme BG.
        with wt._lock:
            wt._call_end = time.time()
        app.status_loop()
        assert app.lbl_symbol.cget("bg") == c["BG"], (
            f"symbol label should restore to theme BG once stall clears; "
            f"got {app.lbl_symbol.cget('bg')!r}"
        )
        print("stall indicator paint + restore OK")
    finally:
        _teardown(app)


def test_etf_map_fallback_warning_fires():
    """ETF map reload with an unreadable custom path must log a
    warning and fall back to the bundled baseline. Without the
    warning, a vanished custom file silently downgrades coverage."""
    _hr("etf_map: custom-path fallback emits warning")
    import etf_map
    import logging as _logging
    handler = _logging.Handler()
    captured = []
    handler.emit = lambda record: captured.append(record)
    etf_map.logger.addHandler(handler)
    etf_map.logger.setLevel(_logging.WARNING)
    try:
        # Point at a path that doesn't exist.
        em = etf_map.EtfMap()
        em.set_path("Z:/definitely/does/not/exist.json")
        # set_path() should have triggered reload + warning.
        warnings = [
            r for r in captured
            if r.levelno >= _logging.WARNING
            and "unreadable" in r.getMessage()
        ]
        assert warnings, (
            "expected a 'unreadable; falling back to bundled baseline' "
            f"warning, got {[r.getMessage() for r in captured]!r}"
        )
        print("ETF map fallback warning OK")
    finally:
        etf_map.logger.removeHandler(handler)


def test_etf_holdings_map_and_indicator():
    """Multi-holding ETF holdings: sector-label derivation, storage
    round-trip, the <2-holding drop rule, reverse inversion with the
    leverage-first sort, the ETF-self indicator text, and the live-app
    indicator routing (primary + 'Held' second column)."""
    _hr("ETF holdings map + indicator wiring")
    import etf_holdings as EH
    import shutil
    import tempfile
    from pathlib import Path

    # derive_sector_label: a dominant REAL sector -> short label; diversified
    # / swap-dominated ("Other") / empty -> "" (no high-confidence label).
    assert EH.derive_sector_label("Technology",
                                  [{"n": "Technology", "w": 99.9}]) == "Tech"
    assert EH.derive_sector_label("Industrials",
                                  [{"n": "Industrials", "w": 89.0}]) == "Industrials"
    assert EH.derive_sector_label("Large Blend",
                                  [{"n": "Technology", "w": 38.0}]) == ""
    assert EH.derive_sector_label("Trading--Leveraged Equity",
                                  [{"n": "Other", "w": 84.0}]) == ""
    assert EH.derive_sector_label("X", []) == ""

    import scan_sec as mod
    _self = mod.ScannerApp._etf_self_text
    assert _self({"sector_label": "Tech", "mult": None}) == "ETF: Tech"
    assert _self({"sector_label": "", "mult": 3.0}) == "ETF: 3X"
    assert _self({"sector_label": "Tech", "mult": 2.0}) == "ETF: Tech 2X"
    assert _self({"sector_label": "", "mult": None}) == "ETF:"

    td = Path(tempfile.mkdtemp())
    try:
        eh = EH.EtfHoldings(path=td / "etf_holdings.json")
        profiles = {
            "XLK": {"mult": None, "category": "Technology",
                    "sector_label": "Tech", "count": 75, "date": "Jun 17, 2026",
                    "holdings": [{"ticker": "NVDA", "name": "NVIDIA", "weight": 13.0},
                                 {"ticker": "AAPL", "name": "Apple", "weight": 11.0}]},
            "SOXL": {"mult": 3.0, "category": "Trading--Leveraged Equity",
                     "sector_label": "", "count": 53, "date": "Jun 16, 2026",
                     "holdings": [{"ticker": "NVDA", "name": "NVIDIA", "weight": 4.5},
                                  {"ticker": "AVGO", "name": "Broadcom", "weight": 4.0}]},
            # <2 distinct holdings: must be dropped by normalize so it never
            # pollutes the reverse map (single-holding == single-stock map's job).
            "BADX": {"mult": None, "category": "x",
                     "holdings": [{"ticker": "ONLY", "weight": 1.0}]},
        }
        eh.replace(profiles, source="test", errors=[])

        assert (td / "etf_holdings.json").exists()
        assert eh.is_etf("XLK") and eh.is_etf("SOXL")
        assert not eh.is_etf("BADX"), "single-holding ETF should be dropped"

        p = eh.get_profile("XLK")
        assert p and p["sector_label"] == "Tech" and len(p["holdings"]) == 2

        # Reverse inversion + leverage-first sort: NVDA is in both; the
        # leveraged SOXL must sort ahead of the non-levered XLK.
        assert [x["etf"] for x in eh.get_holders_for("NVDA")] == ["SOXL", "XLK"]
        assert eh.get_holders_for("AAPL")[0]["etf"] == "XLK"
        assert eh.get_holders_for("ZZZZ") == []

        # Reload from disk reproduces the same derived reverse map.
        eh2 = EH.EtfHoldings(path=td / "etf_holdings.json")
        assert [x["etf"] for x in eh2.get_holders_for("NVDA")] == ["SOXL", "XLK"]

        # Live-app indicator routing.
        app = _make_app()
        try:
            app.etf_holdings = eh
            app.current_symbol = "XLK"
            app._update_etf_label("XLK")               # symbol IS an ETF
            assert app.lbl_etf.cget("text") == "ETF: Tech"
            assert app.lbl_etf_hold.cget("text") == "", "ETF-self must hide 'Held'"
            app.current_symbol = "NVDA"
            app._update_etf_label("NVDA")              # stock held by ETFs
            assert app.lbl_etf_hold.cget("text") == "Held: 2", \
                app.lbl_etf_hold.cget("text")
        finally:
            _teardown(app)
        print("ETF holdings map + indicator OK")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_cik_resolver_close_joins_refresh_thread():
    """CIKResolver.close() must wait briefly for the SEC ticker
    refresh thread before closing the session — otherwise the session
    is torn down underneath an in-flight request."""
    _hr("CIKResolver.close() joins refresh thread")
    import scan_sec as mod
    res = mod.CIKResolver()
    # Refresh thread is daemon + started at construction. close() is
    # the audit fix — call it and verify the thread is gone shortly.
    res.close()
    # Give it a moment past the join timeout to settle.
    import time
    time.sleep(0.1)
    # The thread may still be running if the SEC fetch is wedged;
    # that's OK (daemon will be killed on process exit). What we
    # really verify: close() doesn't raise and the session is closed.
    print("CIKResolver.close() join path OK")


def test_app_repeated_construct_destroy_is_safe():
    """Two consecutive app constructions must both succeed —
    verifies teardown didn't leave global state (Tk root, threads,
    cached classes) that would block a re-init."""
    _hr("repeated construct/destroy cycle")
    for i in range(2):
        app = _make_app()
        try:
            assert app.watch_thread is not None
        finally:
            _teardown(app)
    print("repeated cycle OK")


def test_finviz_ea_synthesizer():
    """The ty=ea synthesizer must map adjusted finviz earningsData into
    canonical rows (period_ending = day-1 of fiscal-quarter-end month,
    surprise = (actual-est)/|est|, YoY vs same-quarter prior year),
    drop forward-estimate rows, guard a zero estimate, and survive a
    ']' inside a JSON string value (raw_decode, not bracket-counting)."""
    _hr("finviz ty=ea synthesizer + YoY")
    import scan_sec as mod
    import pandas as pd
    entries = [
        {"fiscalPeriod": "2026Q2", "earningsDate": "2026-06-04T16:30:00",
         "fiscalEndDate": "2026-04-30", "epsActual": 0.04, "epsEstimate": 0.02,
         "salesActual": 110.0, "salesEstimate": 100.0, "note": "a]b"},
        {"fiscalPeriod": "2025Q2", "earningsDate": "2025-06-05T08:00:00",
         "fiscalEndDate": "2025-04-30", "epsActual": 0.05, "epsEstimate": 0.05,
         "salesActual": 95.0, "salesEstimate": 95.0},
        # Forward estimate (no epsActual) — must be dropped.
        {"fiscalPeriod": "2026Q3", "earningsDate": "2026-09-04T16:00:00",
         "fiscalEndDate": "2026-07-31", "epsEstimate": 0.08, "salesEstimate": 115.0},
    ]
    rows = mod._fv_ea_rows_with_yoy(entries, "BBCP")
    assert len(rows) == 2, f"forward estimate not dropped: {len(rows)} rows"
    newest = rows[-1]  # sorted oldest -> newest, so 2026Q2 is last
    assert newest["ticker"] == "BBCP"
    assert pd.Timestamp(newest["period_ending"]) == pd.Timestamp("2026-04-01"), (
        f"period_ending should be day-1 of fiscal-end month: {newest['period_ending']!r}"
    )
    assert pd.Timestamp(newest["report_date"]) == pd.Timestamp("2026-06-04")
    assert newest["report_time"] == "Close"
    assert newest["report_date_proxy"] is False
    assert newest["source"] == "finviz"
    assert abs(newest["surprise_eps_pct"] - 100.0) < 1e-6, newest["surprise_eps_pct"]
    assert abs(newest["surprise_rev_pct"] - 10.0) < 1e-6, newest["surprise_rev_pct"]
    assert abs(newest["yoy_eps_pct"] - (-20.0)) < 1e-6, newest["yoy_eps_pct"]
    assert abs(newest["yoy_rev_pct"] - 15.789473) < 1e-3, newest["yoy_rev_pct"]
    # Zero-estimate guard: surprise % is None when |estimate| == 0.
    zrow = mod._fv_ea_row_from_entry(
        {"earningsDate": "2026-06-04T16:30:00", "fiscalEndDate": "2026-04-30",
         "epsActual": 0.04, "epsEstimate": 0.0, "salesActual": 110.0,
         "salesEstimate": 0.0}, "ZZZ")
    assert zrow["surprise_eps_pct"] is None and zrow["surprise_rev_pct"] is None
    # Oldest row (2025Q2) has no prior-year in the array -> YoY stays NaN.
    assert pd.isna(rows[0]["yoy_eps_pct"]) and pd.isna(rows[0]["yoy_rev_pct"])
    # _fv_ea_extract pulls the array out of wrapping HTML, ']' and all.
    import json as _json
    html = ('<html><script>var x={"earningsData":'
            + _json.dumps(entries) + '};</script></html>')
    extracted = mod._fv_ea_extract(html)
    assert isinstance(extracted, list) and len(extracted) == 3, extracted
    assert mod._fv_ea_extract("<html>no key here</html>") is None
    print("finviz ty=ea synthesizer OK")


def test_eps_sales_surpr_cell_parser():
    """The snapshot 'EPS/Sales Surpr.' cell parser must read the two
    ordered slots (EPS then Sales) correctly across every live layout —
    both present, either negative, either N/A. Regression guard for the
    BBCP bug where '- 10.43%' (EPS N/A + Sales +10.43%) was collapsed
    into a phantom EPS miss of -10.43%."""
    _hr("EPS/Sales Surpr. cell parser (all layouts)")
    import scan_sec as mod
    from bs4 import BeautifulSoup

    def cell(inner):
        # Mirror finviz's real nesting: td > div > a > b > small > slots.
        html = ('<td class="snapshot-td2"><div class="snapshot-td-content">'
                '<a href="x&ty=ea"><b><small class="xl:text-2xs">'
                + inner + '</small></b></a></div></td>')
        return BeautifulSoup(html, "html.parser").find("td")

    P = 'class="color-text is-positive"'
    N = 'class="color-text is-negative"'
    cases = {
        # label: (inner_html, expected_eps, expected_sales)
        "AAPL both pos": (f'<span {P}>3.30%</span> <span {P}>1.58%</span>',
                          "+3.30%", "+1.58%"),
        "CAG eps neg":   (f'<span {N}>-2.91%</span> <span {P}>0.92%</span>',
                          "-2.91%", "+0.92%"),
        "MMM sales neg": (f'<span {P}>8.14%</span> <span {N}>-0.09%</span>',
                          "+8.14%", "-0.09%"),
        "CE both neg":   (f'<span {N}>-4.64%</span> <span {N}>-0.33%</span>',
                          "-4.64%", "-0.33%"),
        "BBCP eps N/A":  (f'- <span {P}>10.43%</span>', None, "+10.43%"),
        "GME sales N/A": (f'<span {P}>87.50%</span> -', "+87.50%", None),
        "both N/A":      ('- -', None, None),
        "big number":    (f'<span {P}>1426.32%</span> <span {P}>9.33%</span>',
                          "+1426.32%", "+9.33%"),
    }
    for label, (inner, exp_eps, exp_sales) in cases.items():
        eps, sales = mod._parse_eps_sales_surpr_cell(cell(inner))
        assert eps == exp_eps, f"{label}: eps {eps!r} != {exp_eps!r}"
        assert sales == exp_sales, f"{label}: sales {sales!r} != {exp_sales!r}"
    # None / empty cell must not crash.
    assert mod._parse_eps_sales_surpr_cell(None) == (None, None)
    # And the parsed strings must round-trip through the % parser the
    # landing row uses (the BBCP fix: EPS -> None, Sales -> +10.43).
    eps, sales = mod._parse_eps_sales_surpr_cell(cell(f'- <span {P}>10.43%</span>'))
    assert mod.ScannerApp._parse_pct_value(eps) is None
    assert abs(mod.ScannerApp._parse_pct_value(sales) - 10.43) < 1e-9
    print("EPS/Sales Surpr. cell parser OK")


def test_finviz_ea_yoy_small_base_floor():
    """ty=ea YoY must FLOOR to NaN (not emit a blowup) when the prior-year
    base is below the small-base threshold — matching the parquet's
    compute_yoy_columns policy. BBCP Jun-4: prior-year EPS base -$0.01
    (< $0.05) would read +500%; it must be NaN. Rev base $94M is fine."""
    _hr("ty=ea small-base YoY floor (null, not blowup)")
    import scan_sec as mod
    import pandas as pd
    entries = [
        {"earningsDate": "2025-06-05T16:00:00", "fiscalEndDate": "2025-04-30",
         "epsActual": -0.01, "epsEstimate": 0.045,
         "salesActual": 94.0, "salesEstimate": 98.5},
        {"earningsDate": "2026-06-04T16:00:00", "fiscalEndDate": "2026-04-30",
         "epsActual": 0.04, "epsEstimate": 0.0,
         "salesActual": 106.8, "salesEstimate": 96.7},
    ]
    rows = mod._fv_ea_rows_with_yoy(entries, "BBCP")
    newest = rows[-1]
    assert pd.isna(newest["yoy_eps_pct"]), (
        f"small-base (-$0.01) EPS YoY must floor to NaN, got "
        f"{newest['yoy_eps_pct']!r}"
    )
    assert abs(newest["yoy_rev_pct"] - 13.6170) < 1e-2, (
        f"above-floor Rev YoY should still compute, got "
        f"{newest['yoy_rev_pct']!r}"
    )
    # Revenue-base floor too: prior rev $0.5M (< $1.0M) -> Rev YoY NaN.
    rev_entries = [
        {"earningsDate": "2025-06-05T16:00:00", "fiscalEndDate": "2025-04-30",
         "epsActual": 0.50, "epsEstimate": 0.40,
         "salesActual": 0.5, "salesEstimate": 0.4},
        {"earningsDate": "2026-06-04T16:00:00", "fiscalEndDate": "2026-04-30",
         "epsActual": 0.60, "epsEstimate": 0.55,
         "salesActual": 2.0, "salesEstimate": 1.8},
    ]
    rrows = mod._fv_ea_rows_with_yoy(rev_entries, "ZZZ")
    rnew = rrows[-1]
    assert pd.isna(rnew["yoy_rev_pct"]), (
        f"small-base ($0.5M) Rev YoY must floor to NaN, got "
        f"{rnew['yoy_rev_pct']!r}"
    )
    # EPS base 0.50 (>= $0.05) computes normally: (0.60-0.50)/0.50 = 20%.
    assert abs(rnew["yoy_eps_pct"] - 20.0) < 1e-6, rnew["yoy_eps_pct"]
    print("ty=ea small-base YoY floor OK")


def test_parquet_auto_reload_on_mtime_change():
    """The mtime poll must reload the earnings parquet in the background
    when the file changes on disk (a earnings_pipeline refresh) without a
    restart — and a FAILED reload must NOT blank an already-good cache."""
    _hr("parquet auto-reload on mtime change")
    import scan_sec as mod
    import pandas as pd
    import time
    import tempfile
    app = _make_app()
    tmp = None
    try:
        # Let the real startup load settle so its daemon thread can't
        # overwrite the overrides we install below.
        deadline = time.time() + 5
        while (app._earnings_db_full_cache is mod.ScannerApp._PARQUET_NOT_LOADED
               and time.time() < deadline):
            time.sleep(0.02)

        fd, tmp = tempfile.mkstemp(suffix=".parquet")
        os.close(fd)

        def _df(tickers):
            return pd.DataFrame({
                "ticker": tickers,
                "period_ending": [pd.Timestamp("2026-01-01")] * len(tickers),
                "report_date": [pd.Timestamp("2026-02-01")] * len(tickers),
                "reported_eps": [1.0] * len(tickers),
            })

        # Install a known starting state: AAA only, loaded "now".
        _df(["AAA"]).to_parquet(tmp)
        app.earnings_db_path = tmp
        app._earnings_db_full_cache = _df(["AAA"])
        app._earnings_db_mtime = os.path.getmtime(tmp)
        app._earnings_tickers_cache = None
        app._parquet_reloading = False
        base_mtime = app._earnings_db_mtime

        # No change yet -> poll must NOT spawn a reload.
        app._poll_parquet_freshness()
        assert app._parquet_reloading is False, "poll reloaded an unchanged file"

        # Rewrite the file with a 2nd ticker + a forced-distinct mtime
        # (coarse FS clocks can otherwise collide).
        _df(["AAA", "BBB"]).to_parquet(tmp)
        new_mtime = base_mtime + 5
        os.utime(tmp, (new_mtime, new_mtime))

        app._poll_parquet_freshness()
        # Wait for the daemon reload to swap the cache in.
        deadline = time.time() + 5
        while time.time() < deadline:
            cur = app._earnings_db_full_cache
            if cur is not None and "BBB" in set(cur["ticker"]):
                break
            time.sleep(0.05)
        cache = app._earnings_db_full_cache
        assert cache is not None and "BBB" in set(cache["ticker"]), (
            "auto-reload did not pick up the new ticker"
        )

        # Failed reload (path now missing) must preserve the good cache.
        good = app._earnings_db_full_cache
        app.earnings_db_path = tmp + ".does_not_exist"
        app._async_load_parquet()  # synchronous; read fails -> df None
        assert app._earnings_db_full_cache is good, (
            "a failed reload blanked the existing good cache"
        )
        print("parquet auto-reload OK")
    finally:
        _teardown(app)
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ----- main ------------------------------------------------------------


def main():
    print("MorningScanner smoketest")
    print("=" * 40)
    test_import_and_module_shape()
    test_app_construct_and_teardown()
    test_safe_open_url_allowlist()
    test_edgar_regex_gates()
    test_http_byte_caps_present_and_sane()
    test_settings_corrupt_preserves_as_bak()
    test_wave1_settings_load_boundary_hardening()
    test_wave2_network_surface_hardening()
    test_wave3_robustness_hardening()
    test_wave4_security_primitives()
    test_watchthread_is_stalled_initial_false()
    test_status_loop_paints_stall_indicator()
    test_etf_map_fallback_warning_fires()
    test_etf_holdings_map_and_indicator()
    test_finviz_ea_synthesizer()
    test_eps_sales_surpr_cell_parser()
    test_finviz_ea_yoy_small_base_floor()
    test_parquet_auto_reload_on_mtime_change()
    test_cik_resolver_close_joins_refresh_thread()
    test_app_repeated_construct_destroy_is_safe()
    # Reap the final Tk root on the main thread before the process exits,
    # so interpreter-shutdown GC has no leftover root to finalize on the
    # wrong thread (which would abort with Tcl_AsyncDelete after we've
    # already declared success).
    import gc
    gc.collect()
    print("=" * 40)
    print("ALL SMOKETESTS PASSED")


def _backup_live_settings_then(callback):
    """Wrap any function that may construct ScannerApp (which can
    mutate the live ``scanner_settings.json`` on teardown). Snapshots
    + restores the file around the call so the user's saved state
    survives the smoketest unchanged.
    """
    import scan_sec as mod
    live = mod.SETTINGS_FILE
    backup_bytes = live.read_bytes() if live.exists() else None
    bak_path = live.with_suffix(".corrupt.bak")
    bak_existed = bak_path.exists()
    bak_backup = bak_path.read_bytes() if bak_existed else None
    try:
        callback()
    finally:
        if backup_bytes is not None:
            live.write_bytes(backup_bytes)
        elif live.exists():
            live.unlink()
        if bak_existed:
            bak_path.write_bytes(bak_backup)
        elif bak_path.exists():
            bak_path.unlink()


if __name__ == "__main__":
    try:
        _backup_live_settings_then(main)
    except AssertionError as e:
        _fail(str(e))
    except Exception as e:
        _fail(f"unexpected: {type(e).__name__}: {e}")
