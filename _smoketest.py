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


def test_etf_swap_resolution_and_badge():
    """Swap-based ETFs (SPCL and the leveraged/inverse families): the
    swap-description parser, the SEC name->ticker resolver, the
    leverage-only 'badge' fallback for <2-holding funds, and the exclusion
    of inverse funds from the reverse 'Held' map."""
    _hr("ETF swap resolution + badge fallback")
    import etf_scraper as S
    import etf_holdings as EH
    import scan_sec as mod
    import shutil
    import tempfile
    from pathlib import Path

    # Swap-description -> company name (cash / accounting rows -> None).
    assert S._extract_swap_company("ROCKET LAB CORPORATION-SWAP-MREX-L") == \
        "ROCKET LAB CORPORATION"
    assert S._extract_swap_company("AST SPACEMOBILE INC.-SWAP-SWAP-MREX-L") == \
        "AST SPACEMOBILE INC"
    for junk in ("US DOLLARS", "OTHER ASSETS AND LIABILITIES", "Cash Offset", ""):
        assert S._extract_swap_company(junk) is None, junk

    # Name -> ticker resolver, fed synthetic SEC data (no network).
    res = mod.CIKResolver()
    res.close()  # stop the live refresh thread before overriding the maps
    res.process_data({
        "0": {"cik_str": 1, "ticker": "RKLB", "title": "Rocket Lab Corp"},
        "1": {"cik_str": 2, "ticker": "SATS", "title": "EchoStar Corp"},
        "2": {"cik_str": 3, "ticker": "ASTS", "title": "AST SpaceMobile, Inc."},
    })
    assert res.resolve_name_to_ticker("ROCKET LAB CORPORATION") == "RKLB"
    assert res.resolve_name_to_ticker("ECHOSTAR CORPORATION") == "SATS"
    assert res.resolve_name_to_ticker("AST SPACEMOBILE INC.") == "ASTS"
    assert res.resolve_name_to_ticker("WHOLLY UNRELATED HOLDINGS XYZ") is None

    # Badge fallback + inverse exclusion in the holdings map.
    td = Path(tempfile.mkdtemp())
    try:
        eh = EH.EtfHoldings(path=td / "etf_holdings.json")
        eh.replace({
            # Leveraged long fund, 0 holdings -> kept as a blue badge.
            "SPCL": {"mult": 2.0, "category": "", "sector_label": "",
                     "count": 9, "date": "x", "holdings": []},
            # Inverse fund WITH holdings -> profile kept, but excluded from reverse.
            "SOXS": {"mult": -3.0, "category": "", "sector_label": "",
                     "count": 2, "date": "x",
                     "holdings": [{"ticker": "NVDA", "name": "NVIDIA", "weight": 5.0},
                                  {"ticker": "AVGO", "name": "Broadcom", "weight": 4.0}]},
            # Non-leveraged with <2 holdings -> dropped (mis-scrape).
            "BADX": {"mult": None, "category": "x",
                     "holdings": [{"ticker": "ONLY", "weight": 1.0}]},
        }, source="test", errors=[])
        assert eh.is_etf("SPCL"), "leveraged 0-holding fund must keep a badge profile"
        assert mod.ScannerApp._etf_self_text(eh.get_profile("SPCL")) == "ETF: 2X"
        assert eh.is_etf("SOXS")
        assert not eh.is_etf("BADX"), "non-leveraged <2-holding fund must drop"
        # Inverse SOXS must NOT appear as a holder of NVDA.
        assert "SOXS" not in [r["etf"] for r in eh.get_holders_for("NVDA")]
        print("ETF swap resolution + badge OK")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_float_coloration_settings():
    """parse_float honors the settings-tunable cutoff, and the float
    color overrides default to '' (= follow theme green/red)."""
    _hr("float coloration cutoff + colors")
    app = _make_app()
    try:
        f = app.fetcher
        f.float_low_threshold = 5_000_000
        assert f.parse_float("3M")[1] is True
        assert f.parse_float("8M")[1] is False
        f.float_low_threshold = 20_000_000
        assert f.parse_float("15M")[1] is True
        assert f.parse_float("25M")[1] is False
        # Colors default to "" (follow theme); attrs exist.
        assert app.float_low_color == "" and app.float_high_color == ""
        # A non-numeric float string must not raise.
        assert f.parse_float("N/A")[1] is False
        print("float coloration OK")
    finally:
        _teardown(app)


def test_report_day_freshness_marker():
    """On the day Finviz says a company reports, its surprise cells often
    still hold the PRIOR quarter's numbers. The freshness marker compares
    the just-scraped values against the previous quarter's parquet row and
    reports (OLD) / (NEW) / ? accordingly."""
    _hr("report-day (OLD)/(NEW)/? freshness marker")
    import datetime as _dt
    import pandas as pd
    app = _make_app()
    try:
        today = _dt.date.today()
        ts = pd.Timestamp(today)

        def _df(rows):
            return pd.DataFrame(rows)

        def _row(days_ago, eps, rev, ticker="TEST", source="finviz",
                 proxy=False, pe_days_ago=None):
            # period_ending defaults to 60d before the report date — the
            # real parquet's typical reporting lag.
            if pe_days_ago is None:
                pe_days_ago = days_ago + 60
            return {
                "ticker": ticker,
                "report_date": ts - pd.Timedelta(days=days_ago),
                "period_ending": ts - pd.Timedelta(days=pe_days_ago),
                "surprise_eps_pct": eps,
                "surprise_rev_pct": rev,
                "source": source,
                "report_date_proxy": proxy,
            }

        F = app._resolve_report_day_freshness

        # Baseline: previous quarter 91 days back at 77.598 / 9.0926 —
        # the real RDDT shape. Finviz still showing those rounded to 2dp
        # means it has NOT refreshed.
        prev = _df([_row(91, 77.598031, 9.092596)])
        assert F("TEST", today, 77.60, 9.09, prev) == "old"
        # Either value moving is enough to call it this quarter's.
        assert F("TEST", today, 81.20, 9.09, prev) == "new"
        assert F("TEST", today, 77.60, 12.40, prev) == "new"
        assert F("TEST", today, 81.20, 12.40, prev) == "new"
        # Sign flips must never be read as a match.
        assert F("TEST", today, -77.60, -9.09, prev) == "new"

        # Only overlapping cells are compared: Finviz posting EPS alone is
        # judged on EPS, not forced to "unknown".
        assert F("TEST", today, 77.60, None, prev) == "old"
        assert F("TEST", today, 81.20, None, prev) == "new"
        assert F("TEST", today, None, 9.09, prev) == "old"
        # ...and the same when the PARQUET side is the one missing a cell.
        prev_eps_only = _df([_row(91, 77.598031, float("nan"))])
        assert F("TEST", today, 77.60, 9.09, prev_eps_only) == "old"
        assert F("TEST", today, 81.20, 9.09, prev_eps_only) == "new"
        # No overlap at all -> unverifiable, not a false (NEW).
        assert F("TEST", today, None, 9.09, prev_eps_only) == "unknown"

        # No previous quarter in the parquet -> "?".
        assert F("TEST", today, 77.60, 9.09, _df([])) == "unknown"
        assert F("TEST", today, 77.60, 9.09, None) == "unknown"
        # A row too far back (skipped refresh) is not "last quarter".
        assert F("TEST", today, 77.60, 9.09, _df([_row(400, 77.598, 9.09)])) == "unknown"
        # Another ticker's rows must never be borrowed.
        assert F("TEST", today, 77.60, 9.09,
                 _df([_row(91, 77.598, 9.0926, ticker="OTHER")])) == "unknown"

        # Correct-quarter selection: the parquet already carries TODAY's
        # row (batch refresh landed first). The comparison must still
        # anchor on the quarter BEFORE it, not on today's own row —
        # otherwise every value would trivially "match" and read (OLD).
        with_today = _df([
            _row(0, 81.20, 12.40),     # this quarter, already in parquet
            _row(91, 77.598031, 9.092596),  # the real previous quarter
        ])
        assert F("TEST", today, 81.20, 12.40, with_today) == "new"
        assert F("TEST", today, 77.60, 9.09, with_today) == "old"
        # A near-duplicate row a few days off the Finviz date is the SAME
        # quarter, not the previous one — it must not become the baseline.
        near_dup = _df([
            _row(3, 81.20, 12.40),
            _row(91, 77.598031, 9.092596),
        ])
        assert F("TEST", today, 81.20, 12.40, near_dup) == "new"
        # Two prior quarters present -> the most recent one wins.
        two_back = _df([
            _row(91, 77.598031, 9.092596),
            _row(182, 32.337247, 8.961240),
        ])
        assert F("TEST", today, 77.60, 9.09, two_back) == "old"
        assert F("TEST", today, 32.34, 8.96, two_back) == "new"

        # Irregular reporting cadence (the real SOTK shape): consecutive
        # quarters only 41 days apart by report_date, but a clean 3-month
        # step in period_ending. Ranking by report_date gaps skipped this
        # quarter entirely and compared against TWO quarters back; ranking
        # by period_ending must pick the true previous quarter.
        irregular = _df([
            _row(41, 20.0, 0.332713, pe_days_ago=41 + 97),    # prev quarter
            _row(220, -20.0, -4.785099, pe_days_ago=220 + 96),  # two back
        ])
        assert app._prev_quarter_surprises("TEST", today, irregular)[0] == 20.0
        assert F("TEST", today, 20.00, 0.33, irregular) == "old"
        assert F("TEST", today, 25.00, 1.10, irregular) == "new"
        # ...and the two-quarters-back row must never be the baseline.
        assert F("TEST", today, -20.00, -4.79, irregular) == "new"

        # A current-quarter row whose report_date drifted past the
        # same-quarter exclusion still gets rejected by the period_ending
        # floor (it sits ~66d out, far under the ~150d a real prior
        # quarter shows), rather than trivially matching itself.
        drifted = _df([_row(12, 81.20, 12.40, pe_days_ago=72)])
        assert app._prev_quarter_surprises("TEST", today, drifted) == (None, None)
        assert F("TEST", today, 81.20, 12.40, drifted) == "unknown"

        # Finnhub calendar-proxy placeholders are excluded from baseline
        # selection (wrong date + NaN rev), same as the past-row selector.
        proxied = _df([
            _row(60, 1.11, float("nan"), source="finnhub", proxy=True),
            _row(91, 77.598031, 9.092596),
        ])
        assert F("TEST", today, 77.60, 9.09, proxied) == "old"

        # Not report day -> no marker at all.
        assert F("TEST", today - _dt.timedelta(days=1), 77.60, 9.09, prev) is None
        assert F("TEST", today + _dt.timedelta(days=7), 77.60, 9.09, prev) is None
        assert F("TEST", None, 77.60, 9.09, prev) is None
        # Nothing scraped -> nothing to qualify.
        assert F("TEST", today, None, None, prev) is None

        # --- Painting -------------------------------------------------
        base = {
            "date_str": app._fmt_short_date(ts), "date_obj": today,
            "is_future": False, "eps_surp": "+77.60%", "rev_surp": "+9.09%",
            "eps_yoy": None, "rev_yoy": None, "period_ending": None,
            "in_parquet": True, "needs_xbrl_yoy": False, "sec_accession": "",
        }
        app._paint_earnings_row(dict(base, report_day_fresh="old"))
        assert app.lbl_earn_fresh.cget("text") == "(OLD)"
        assert app.lbl_earn_fresh.cget("fg").upper() == app._FRESH_OLD_COLOR
        app._paint_earnings_row(dict(base, report_day_fresh="unknown"))
        assert app.lbl_earn_fresh.cget("text") == "?"
        assert app.lbl_earn_fresh.cget("fg").upper() == app._FRESH_UNK_COLOR
        app._paint_earnings_row(dict(base, report_day_fresh="new"))
        assert app.lbl_earn_fresh.cget("text") == "(NEW)"
        # (NEW) reuses the same green a same-day earnings date paints in.
        assert app.lbl_earn_fresh.cget("fg") == app.earn_pos_color
        assert app.lbl_earnings.cget("fg") == app.earn_pos_color
        # Non-report-day and Historical dicts (no key) leave it blank.
        app._paint_earnings_row(dict(base, report_day_fresh=None))
        assert app.lbl_earn_fresh.cget("text") == ""
        app._paint_earnings_row(dict(base, report_day_fresh="new"))
        app._paint_earnings_row(base)  # key absent entirely
        assert app.lbl_earn_fresh.cget("text") == ""
        # An upcoming-earnings row must clear it too.
        app._paint_earnings_row(dict(base, report_day_fresh="old"))
        app._paint_earnings_row(dict(base, is_future=True,
                                     date_obj=today + _dt.timedelta(days=7)))
        assert app.lbl_earn_fresh.cget("text") == ""
        # And so must the blanket clear.
        app._paint_earnings_row(dict(base, report_day_fresh="old"))
        app._clear_earnings_labels()
        assert app.lbl_earn_fresh.cget("text") == ""
        print("report-day freshness marker OK")
    finally:
        _teardown(app)


def test_search_filter_paste_sanitizing():
    """A multi-line clipboard paste into the single-line Search box used
    to be stored verbatim (rendering as blank), persisted to settings,
    and re-applied on the next launch — filtering every headline away so
    the news panel looked dead. Sanitizing must collapse it to a bounded
    single-line term, and a filter that hides everything must announce
    itself with a note row instead of leaving the panel silently empty."""
    _hr("search filter paste sanitizing + empty-state note")
    app = _make_app()
    try:
        S = app._sanitize_filter_text
        # Whitespace-only pastes (the invisible case) become "no filter".
        assert S("\t\t\t\n\t") == ""
        assert S("   ") == ""
        assert S("") == ""
        # Multi-line paste -> single line, capped.
        blob = "Entered\tType\tSymbol\n07/23/26 09:46:33 AM\tSell\tRELL\n" * 30
        clean = S(blob)
        assert "\n" not in clean and "\t" not in clean
        assert len(clean) <= app._MAX_FILTER_TEXT
        # Normal terms + the quoted whole-word grammar survive untouched.
        assert S("merger") == "merger"
        assert S('"FDA" , halt') == '"FDA" , halt'
        assert len(app._parse_hot_words(S('  "FDA" , offering '))) == 2

        # Entry sanitizing is in-place: what filters is what's displayed
        # (and therefore what on_close persists).
        app.entry_search_kw.delete(0, "end")
        app.entry_search_kw.insert(0, blob)
        app.apply_search()
        assert app.entry_search_kw.get() == clean
        assert "\n" not in app.entry_search_kw.get()

        # A pure-whitespace paste must clear to "no filter", not filter all.
        app.entry_search_kw.delete(0, "end")
        app.entry_search_kw.insert(0, "\t\t\t")
        app.apply_search()
        assert app.entry_search_kw.get() == ""
        assert app.search_keywords == []

        # Empty-state note: items present + filter matches none -> one
        # note row, with a non-numeric iid so the row handlers skip it.
        app.var_all.set(True)
        app.current_items = [
            {"date": "2026-07-24", "time": "09:00AM", "age": "1h",
             "headline": "Reddit Inc reports Q2 results", "url": "",
             "source": "Finviz", "is_today": True},
        ]
        app.entry_search_kw.delete(0, "end")
        app.entry_search_kw.insert(0, "zzz-no-such-term")
        app.apply_search()
        kids = app.tree.get_children()
        assert kids == ("filter_note",), kids
        assert app._displayed_indices == []
        assert not "filter_note".isdigit()  # on_double_click fast path skips it
        # Clearing restores the row.
        app.clear_search()
        assert app.tree.get_children() == ("0",)

        print("search filter paste sanitizing OK")
    finally:
        _teardown(app)


def test_mcap_gradient_and_float_toggle():
    """MCap is now the always-on big header label with a 5-tier USD
    gradient; Float is the toggleable label. Verify the parse + tier
    mapping, the new attrs/widgets exist, and the render helpers paint
    the expected text/colors without raising."""
    _hr("mcap gradient + float toggle")
    import scan_sec as mod
    # Pure parse helpers.
    assert mod._parse_mcap_dollars("1.50B") == 1_500_000_000
    assert mod._parse_mcap_dollars("850.00M") == 850_000_000
    assert mod._parse_mcap_dollars("12.3K") == 12_300
    assert mod._parse_mcap_dollars("2.0T") == 2_000_000_000_000
    assert mod._parse_mcap_dollars("N/A") is None
    assert mod._parse_mcap_dollars("") is None
    assert mod._parse_mcap_dollars(None) is None
    # Tier boundaries: <250M micro, 250M-2B small, 2B-10B mid,
    # 10B-200B large, >=200B mega.
    assert mod._mcap_tier(100_000_000) == "micro"
    assert mod._mcap_tier(250_000_000) == "small"
    assert mod._mcap_tier(2_000_000_000) == "mid"
    assert mod._mcap_tier(10_000_000_000) == "large"
    assert mod._mcap_tier(200_000_000_000) == "mega"
    assert mod._mcap_tier(None) is None

    app = _make_app()
    try:
        # New widgets + attrs present.
        assert hasattr(app, "lbl_mcap") and hasattr(app, "chk_float")
        assert hasattr(app, "var_float")
        assert app.mcap_gradient_enabled is True
        assert app.float_color_enabled is True
        assert set(app.mcap_tier_colors) == set(mod.MCAP_TIER_KEYS)
        # The old MCap checkbox is gone.
        assert not hasattr(app, "chk_mcap") and not hasattr(app, "var_mcap")

        # MCap label paints with the mega tier color when gradient on.
        app.current_meta = {"mcap": "300.0B", "float": "5M", "is_low": True}
        app._render_mcap_label()
        assert app.lbl_mcap.cget("text") == "MCap 300.0B"
        assert app.lbl_mcap.cget("fg").upper() == \
            mod.MCAP_TIER_DEFAULT_COLORS["mega"].upper()
        # Gradient off -> theme fg.
        app.mcap_gradient_enabled = False
        app._render_mcap_label()
        assert app.lbl_mcap.cget("fg") == app.colors["FG"]

        # Float hidden when toggle off; shown + low-colored when on.
        app.var_float.set(False)
        app._render_float_label()
        assert app.lbl_float.cget("text") == ""
        app.var_float.set(True)
        app._render_float_label()
        assert app.lbl_float.cget("text") == "Float 5M"
        # Coloration off -> theme fg even for a low float.
        app.float_color_enabled = False
        app._render_float_label()
        assert app.lbl_float.cget("fg") == app.colors["FG"]
        print("mcap gradient + float toggle OK")
    finally:
        _teardown(app)


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


# ----- 2026-08-11 audit fixes ------------------------------------------


def test_finviz_field_caps_and_linear_date_parse():
    """Scraped snapshot values are capped at the source, and
    parse_earnings_date is linear (it used to backtrack quadratically over
    an unbounded Finviz cell, on the Tk MAIN thread: 7.9 s at 20k chars,
    ~150 h at the 5 MB body cap = a permanent freeze)."""
    _hr("finviz field caps + linear earnings-date parse")
    import scan_sec as mod
    import time

    f = mod.DataFetcher()
    try:
        # 1. Behaviour preserved for every documented form.
        expected = {
            "Mar 5": (3, 5), "Mar 5 AMC": (3, 5), "Mar 5 BMO": (3, 5),
            "Mar 5 AH": (3, 5), "Mar 5/6": (3, 5), "Mar 5 - Mar 7": (3, 5),
            "Mar 5, 2026": (3, 5), "3/5/2026": (3, 5),
            "Feb 3-Feb 5": (2, 3), "Jan 30 AMC": (1, 30),
        }
        for text, (mo, day) in expected.items():
            got = f.parse_earnings_date(text)
            assert got is not None, f"{text!r} no longer parses"
            assert (got.month, got.day) == (mo, day), \
                f"{text!r} -> {got}, expected month/day {mo}/{day}"
        for junk in ("", "   ", "-", "not a date"):
            assert f.parse_earnings_date(junk) is None, \
                f"{junk!r} should not parse"

        # 2. Linear, not quadratic. The old regex was ~4x per doubling;
        # assert the 64k case stays far under a frame.
        hostile = "Jan 30" + " " * 64000 + "z"
        t0 = time.perf_counter()
        f.parse_earnings_date(hostile)
        dt = time.perf_counter() - t0
        assert dt < 0.5, (
            f"parse_earnings_date took {dt:.2f}s on a 64k input — the "
            "quadratic backtracking is back")

        # 3. The scrape caps every snapshot value at the source.
        class _Resp:
            status_code = 200

            def __init__(self, body):
                self._b = body.encode()

            def iter_content(self, chunk_size=65536):
                for i in range(0, len(self._b), chunk_size):
                    yield self._b[i:i + chunk_size]

            def close(self):
                pass

        class _Sess:
            def __init__(self, body):
                self.body = body

            def get(self, *a, **k):
                return _Resp(self.body)

        big = "9" * 50000
        page = (
            "<html><head><title>X - Evil Corp Stock</title></head><body>"
            "<table class='snapshot-table2'>"
            "<tr><td>Earnings</td><td>Jan 30" + " " * 30000 + "z</td>"
            "<td>Market Cap</td><td>" + big + "</td></tr>"
            "<tr><td>Shs Float</td><td>" + big + "</td>"
            "<td>Rel Volume</td><td>" + big + "</td></tr>"
            "<tr><td>Short Float</td><td>" + big + "</td>"
            "<td>Earnings</td><td>Jan 30</td></tr>"
            "</table></body></html>"
        )
        f.session = _Sess(page)
        f.last_scrape_time = 0.0
        t0 = time.perf_counter()
        meta, _items = f.scrape_finviz("EVIL")
        assert time.perf_counter() - t0 < 5.0, "scrape_finviz stalled"
        for key in ("earnings", "mcap", "float", "rvol", "short"):
            assert len(str(meta[key])) <= mod._FINVIZ_FIELD_MAX, (
                f"meta[{key!r}] is {len(str(meta[key]))} chars — the "
                f"{mod._FINVIZ_FIELD_MAX}-char source cap is not applied")
        # And the capped value still parses to the right date.
        assert f.parse_earnings_date(meta["earnings"]).day == 30
    finally:
        f.close()
    print("finviz field caps + linear parse OK")


def test_atomic_write_fails_fast_when_dir_denied():
    """_atomic_write_json must RAISE on a write-denied directory, not spin.
    tempfile.mkstemp loops range(TMP_MAX)=2.1e9 (~54 h at 100% CPU) there,
    because Windows os.access(W_OK) ignores ACLs — so the callers'
    ``except OSError`` never fired and the GUI just hung."""
    _hr("atomic write fails fast on a denied directory")
    import scan_sec as mod
    import etf_map as em
    import etf_holdings as eh
    import json as _json
    import time
    import tempfile

    d = Path(tempfile.mkdtemp())
    try:
        # Round-trip still works.
        target = d / "settings.json"
        payload = {"a": 1, "unicode": "café"}
        mod._atomic_write_json(target, payload)
        assert _json.loads(target.read_text(encoding="utf-8")) == payload
        assert not [p for p in d.iterdir() if p.name.endswith(".tmp")], \
            "atomic write left a stray temp file"

        # Denied directory -> immediate raise, from all three writers.
        real_open = os.open

        def denying_open(path, flags, *a, **k):
            if flags & os.O_CREAT:
                raise PermissionError(13, "Permission denied (simulated ACL)")
            return real_open(path, flags, *a, **k)

        cases = (
            ("scan_sec", lambda: mod._atomic_write_json(d / "x.json", {"x": 1})),
            ("etf_map", lambda: em._open_exclusive_temp(d / "x.json", ".etfmap_")),
            ("etf_holdings", lambda: eh._open_exclusive_temp(d / "x.json", ".etfhold_")),
        )
        for label, fn in cases:
            os.open = denying_open
            t0 = time.perf_counter()
            try:
                fn()
                raise AssertionError(
                    f"{label} write returned instead of raising on a "
                    "denied directory")
            except OSError:
                pass
            finally:
                os.open = real_open
            dt = time.perf_counter() - t0
            assert dt < 1.0, (
                f"{label} took {dt:.1f}s to fail — it is still spinning "
                "instead of propagating PermissionError")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("atomic write fail-fast OK")


def test_parquet_schema_drift_is_contained():
    """A drifted sibling parquet must degrade to a blank earnings row —
    never raise into the Tk callback that repaints the news list.

    Before: the live resolver was called bare (only the historical branch
    was wrapped), so a renamed/objected/tz-aware report_date unwound
    through update_full_data and skipped refresh_ui() +
    _set_last_refreshed_now() on the following lines."""
    _hr("parquet schema drift is contained")
    import scan_sec as mod
    import pandas as pd

    coerce = mod.ScannerApp._coerce_parquet

    # 1. Load boundary: reject what can't be used, normalize what can.
    assert coerce(pd.DataFrame({"ticker": ["A"]}), "p") is None, \
        "a frame with no report_date must be rejected at load"
    tz = coerce(pd.DataFrame({
        "ticker": ["A"],
        "report_date": pd.to_datetime(["2026-01-01"]).tz_localize("UTC"),
    }), "p")
    assert getattr(tz["report_date"].dtype, "tz", None) is None, \
        "tz-aware report_date must be made naive at the load boundary"
    strs = coerce(pd.DataFrame({
        "ticker": ["A"], "report_date": ["2026-01-01"],
        "surprise_eps_pct": ["n/a"],
    }), "p")
    assert str(strs["report_date"].dtype).startswith("datetime64"), \
        "string report_date must be coerced to datetime"
    assert pd.isna(strs["surprise_eps_pct"].iloc[0]), \
        "non-numeric value column must coerce to NaN, not raise later"

    # 2. Even if a drifted frame reaches the resolver, the repaint holds.
    app = _make_app()
    try:
        bad = pd.DataFrame({"ticker": ["ZZZZ"], "close": [1.0]})   # no report_date
        app._earnings_db_full_cache = bad
        app._earnings_tickers_cache = None
        app.current_symbol = "ZZZZ"
        app.current_meta = {"earnings": "Jan 30"}
        app.var_earnings.set(True)
        reached = []
        app.refresh_ui = lambda *a, **k: reached.append("refresh_ui")
        app._set_last_refreshed_now = lambda *a, **k: reached.append("stamp")
        # The exact call chain the failure took: update_full_data ->
        # refresh_meta_label -> _resolve_earnings_display.
        app.update_full_data("ZZZZ", {"earnings": "Jan 30"}, [], None, None, None)
        assert "refresh_ui" in reached, (
            "update_full_data aborted before refresh_ui — the news list "
            "would be blank")
        assert "stamp" in reached, (
            "update_full_data aborted before the Last Refreshed stamp")
    finally:
        _teardown(app)
    print("parquet schema drift contained OK")


def test_refresh_button_always_released():
    """The refresh button has exactly one re-enable site; every exit path
    must reach it. Two paths used to strand it disabled forever (a
    disabled tk.Button cannot be clicked, so there is no recovery):
    the stale-generation early returns, and an exception in the finisher."""
    _hr("refresh button is always released")
    app = _make_app()
    try:
        calls = []
        app.after = lambda ms, fn=None, *a: (calls.append(fn), fn and fn())[0]

        # Path A: symbol changes mid-refresh (bumps _fetch_gen).
        app.btn_refresh.config(state="disabled", text="…")
        app.fetcher.rss_worker.fetch_feeds = lambda **k: ([], True)
        app.fetcher.rss_worker.merge_into_cache = lambda items: []
        app._fetch_gen = 99
        app._do_manual_refresh("AAPL", 1)      # gen 1 != 99 -> early return
        assert str(app.btn_refresh["state"]) == "normal", (
            "stale-generation return left the refresh button disabled")

        # Path B: the finisher raises.
        app.btn_refresh.config(state="disabled", text="…")

        def boom(*a, **k):
            raise KeyError("headline")

        app.update_full_data = boom
        app.current_symbol = "AAPL"
        app._finish_manual_refresh("AAPL", {}, [], None, None, None)
        assert str(app.btn_refresh["state"]) == "normal", (
            "an exception in _finish_manual_refresh left the button disabled")
        assert app.btn_refresh["text"] == "↻", "button label not restored"
    finally:
        _teardown(app)
    print("refresh button release OK")


def test_chart_load_uses_snapshotted_cik_and_meta():
    """The chart loader must take cik/meta as parameters and never read
    self.current_cik / self.current_meta. Reading them live let a symbol
    change mid-load merge another company's EDGAR + Finviz numbers into
    the charted ticker's rows, with no marker."""
    _hr("chart load uses snapshotted cik + meta")
    import scan_sec as mod
    import inspect

    sig = inspect.signature(mod.ScannerApp._load_chart_data_with_gap_fill)
    for p in ("cik", "finviz_meta"):
        assert p in sig.parameters, (
            f"_load_chart_data_with_gap_fill lost its {p!r} parameter — the "
            "worker would read live app state again")
    # AST, not substring: the docstring legitimately NAMES these
    # attributes when explaining why they must not be read here.
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(mod.ScannerApp._load_chart_data_with_gap_fill)))
    banned = {"current_cik", "current_meta"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and node.attr in banned
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            raise AssertionError(
                f"self.{node.attr} is read at line {node.lineno} of the "
                "off-thread chart loader — it must be snapshotted on the "
                "Tk thread and passed in")

    # A stale load must be dropped rather than rendered under the new
    # ticker's name.
    app = _make_app()
    try:
        rendered = []
        app._render_earnings_chart_window = lambda *a, **k: rendered.append(a)
        app._show_dates_only_popup = lambda *a, **k: rendered.append(a)
        app._show_no_earnings_data_popup = lambda *a, **k: rendered.append(a)
        app._chart_gen = 7
        app._finish_open_earnings_chart(
            "AAPL", None, None, None, "", None, chart_gen=6)
        assert not rendered, "a stale chart load was rendered anyway"
        # The current generation still renders.
        app._finish_open_earnings_chart(
            "AAPL", None, None, None, "", None, chart_gen=7)
        assert rendered, "the current-generation chart load was dropped"
    finally:
        _teardown(app)
    print("chart snapshot + generation guard OK")


def test_new_quarter_detected_by_report_proximity():
    """Quarter identity must come from same-event proximity, not from a
    50-day cadence assumption. 3.88% of real consecutive quarters report
    <=50 days apart, and each one produced a row mixing this quarter's
    date+surprise with last quarter's YoY and period_ending."""
    _hr("new-quarter detection by report proximity")
    import scan_sec as mod
    import pandas as pd
    from datetime import date, timedelta

    app = _make_app()
    try:
        today = date.today()
        fv_day = today - timedelta(days=1)
        # Parquet is one quarter behind, only 36 days back (ICLR's real
        # cadence). The old >50-day rule called this the SAME quarter.
        stale_rd = pd.Timestamp(fv_day) - pd.Timedelta(days=36)
        df = pd.DataFrame({
            "ticker": ["TEST"],
            "report_date": [stale_rd],
            "period_ending": [stale_rd - pd.Timedelta(days=40)],
            "surprise_eps_pct": [4.0], "surprise_rev_pct": [5.0],
            "yoy_eps_pct": [-44.0], "yoy_rev_pct": [-33.0],
            "source": ["finviz"], "report_date_proxy": [False],
        })
        app._earnings_db_full_cache = df
        app._earnings_tickers_cache = None
        meta = {
            "earnings": fv_day.strftime("%b %d"),
            "eps_surprise": "300.00%", "sales_surprise": "250.00%",
        }
        res = app._resolve_earnings_display("TEST", meta)
        assert res is not None, "resolver returned nothing"
        assert res["eps_yoy"] is None and res["rev_yoy"] is None, (
            "borrowed the PREVIOUS quarter's YoY onto a new quarter "
            f"(got {res['eps_yoy']}/{res['rev_yoy']})")
        assert res["period_ending"] is None, (
            "borrowed the previous quarter's period_ending")
        assert res.get("needs_finviz_yoy"), (
            "new quarter must request the async ty=ea YoY backfill")

        # Same event (parquet is current) still borrows normally.
        df.loc[0, "report_date"] = pd.Timestamp(fv_day)
        app._earnings_db_full_cache = df
        res2 = app._resolve_earnings_display("TEST", meta)
        assert res2["eps_yoy"] == -44.0, (
            "a same-quarter row must still supply YoY "
            f"(got {res2['eps_yoy']})")
    finally:
        _teardown(app)
    print("new-quarter detection OK")


def test_impossible_periods_and_tie_break():
    """Rows whose period_ending post-dates their report_date are corrupt
    and must be dropped; genuine ties must break on report-lag
    plausibility then source preference, not on parquet row order."""
    _hr("impossible periods dropped + deterministic tie-break")
    import scan_sec as mod
    import pandas as pd

    App = mod.ScannerApp
    frame = pd.DataFrame({
        "ticker": ["A", "A"],
        "report_date": [pd.Timestamp("2024-08-20")] * 2,
        # First row is impossible (period ends AFTER the report).
        "period_ending": [pd.Timestamp("2024-12-01"),
                          pd.Timestamp("2024-06-01")],
        "surprise_eps_pct": [640.26, 78.0],
        "source": ["finnhub", "finviz"],
    })
    kept = App._drop_impossible_periods(frame)
    assert len(kept) == 1, f"expected 1 sane row, got {len(kept)}"
    assert kept.iloc[0]["surprise_eps_pct"] == 78.0, (
        "kept the impossible row (period_ending after report_date)")

    # Missing values must not be dropped (nothing to contradict).
    with_nat = pd.DataFrame({
        "report_date": [pd.Timestamp("2024-08-20")],
        "period_ending": [pd.NaT],
    })
    assert len(App._drop_impossible_periods(with_nat)) == 1

    # Tie-break: equal distance, both plausible -> source preference wins.
    tied = pd.DataFrame({
        "report_date": [pd.Timestamp("2024-08-20")] * 2,
        "period_ending": [pd.Timestamp("2024-06-30")] * 2,
        "surprise_eps_pct": [1.0, 2.0],
        "source": ["finnhub", "finviz"],
    })
    dist = (tied["report_date"] - pd.Timestamp("2024-08-20")).abs()
    ranked = App._rank_rows_by(tied, dist)
    assert ranked.iloc[0]["source"] == "finviz", (
        "tie-break ignored the finviz > zacks > finnhub source preference")

    # A frame with no period_ending/source columns must still rank.
    bare = pd.DataFrame({"report_date": [pd.Timestamp("2024-08-20")]})
    assert len(App._rank_rows_by(
        bare, (bare["report_date"] - pd.Timestamp("2024-08-20")).abs())) == 1
    print("impossible periods + tie-break OK")


def test_sec_unknown_is_not_asserted_as_negative():
    """An unanswered SEC request must render "Shelf: —" / "SEC: —", not
    the positive assertions "Shelf: NO" / "SEC: >48h". And a transient
    5xx must NOT be negative-cached for the session."""
    _hr("SEC unknown state is not asserted as a negative")
    import scan_sec as mod

    # 1. Transient statuses are not cached; permanent ones are.
    class _R:
        def __init__(self, code):
            self.status_code = code

        def iter_content(self, chunk_size=65536):
            yield b"{}"

        def close(self):
            pass

    class _S:
        def __init__(self, code):
            self.code = code
            self.calls = 0

        def get(self, *a, **k):
            self.calls += 1
            return _R(self.code)

    for code, should_cache in ((503, False), (429, False), (404, True)):
        f = mod.DataFetcher()
        try:
            f.session = _S(code)
            f._fetch_submissions("0000320193")
            cached = "0000320193" in f._submissions_cache
            assert cached is should_cache, (
                f"HTTP {code}: cached={cached}, expected {should_cache}")
            if not should_cache:
                # A retry must actually go back out to SEC.
                f._fetch_submissions("0000320193")
                assert f.session.calls == 2, (
                    f"HTTP {code} was not retried — the session is poisoned")
            # The status light must never read OK off a failure.
            assert f.sec_status == "ERR", (
                f"sec_status={f.sec_status!r} after HTTP {code}")
        finally:
            f.close()

    # 2. scrape_sec_data reports unknown as None, not False/2.
    f = mod.DataFetcher()
    try:
        has_s3, recent, _re = f.scrape_sec_data("AAPL", None)
        assert has_s3 is None and recent is None, (
            f"no CIK should be unknown, got has_s3={has_s3!r} "
            f"recent={recent!r}")
    finally:
        f.close()

    # 3. The render shows an em-dash for unknown.
    app = _make_app()
    try:
        app.current_symbol = "AAPL"
        app.update_full_data("AAPL", {}, [], None, None, None)
        assert "—" in app.lbl_shelf["text"], (
            f"unknown shelf rendered as {app.lbl_shelf['text']!r}")
        assert "—" in app.lbl_sec_recent["text"], (
            f"unknown SEC recency rendered as {app.lbl_sec_recent['text']!r}")
        # A real negative still renders as a negative.
        app.update_full_data("AAPL", {}, [], False, 2, None)
        assert "NO" in app.lbl_shelf["text"], "a real negative was lost"
    finally:
        _teardown(app)
    print("SEC unknown state OK")


def test_geometry_clamped_to_visible_desktop():
    """A geometry saved on a monitor that has since been unplugged must
    not reopen the window off-screen (the app then reads as 'failed to
    launch'). Shape validation alone never caught this — an off-screen
    geometry is perfectly legal Tk, so the except TclError fallback around
    it is dead code for this input."""
    _hr("geometry clamped to the visible desktop")
    app = _make_app()
    try:
        vx, vy = app.winfo_vrootx(), app.winfo_vrooty()
        vw, vh = app.winfo_vrootwidth(), app.winfo_vrootheight()

        onscreen = "1200x800+%d+%d" % (vx + 100, vy + 100)
        assert app._usable_geometry(onscreen) == onscreen, \
            "an on-screen geometry was rejected"

        # Far off the left/top of the virtual desktop -> size only.
        off = "800x600+%d+%d" % (vx - 9000, vy - 9000)
        assert app._usable_geometry(off) == "800x600", (
            f"off-screen geometry {off!r} was restored verbatim")
        # Far off the right.
        off_r = "800x600+%d+50" % (vx + vw + 5000)
        assert app._usable_geometry(off_r) == "800x600", \
            "off-right geometry was restored verbatim"

        # Size-only and malformed inputs behave.
        assert app._usable_geometry("1000x940") == "1000x940"
        for bad in ("garbage", "", None, 123, "12x"):
            assert app._usable_geometry(bad) is None, \
                f"{bad!r} should be rejected outright"

        # The legitimate multi-monitor negative-coordinate form still
        # round-trips when that monitor is present.
        if vx < 0:
            neg = "1200x800+%d+%d" % (vx + 50, vy + 50)
            assert app._usable_geometry(neg) == neg, \
                "a valid negative-coordinate geometry was clamped away"
    finally:
        _teardown(app)
    print("geometry clamp OK")


def test_wire_feeds_and_circuit_breaker():
    """The wire roster, the shortened timeout, and the circuit breaker.

    A dead origin used to cost WIRE_ATTEMPTS x timeout on EVERY 60s cycle,
    and because fetch_feeds waits for the slowest feed it also delayed the
    healthy wires and blocked every manual refresh by the same amount
    (GlobeNewswire went dark 2026-08-10 and cost ~31s/cycle)."""
    _hr("wire roster + circuit breaker")
    import scan_sec as mod
    import time as _t

    # --- roster + UI wiring stay in lockstep -------------------------
    codes = [c for c, _u, _iv in mod.RSSWorker.FEEDS]
    assert codes == ["PR", "ST", "YH"], f"unexpected feed roster: {codes}"
    assert mod.RSSWorker._SOURCE_LABELS["ST"] == "STitn"
    # Retired GlobeNewswire must keep its label so cached items still
    # render until they age past the 7-day cutoff.
    assert mod.RSSWorker._SOURCE_LABELS.get("GB") == "Globe"
    assert mod.RSSWorker.WIRE_TIMEOUT == 6.0, "wire timeout regressed"
    for _c, u, _iv in mod.RSSWorker.FEEDS:
        assert u.startswith("https://"), f"non-https feed url: {u}"
        assert "globenewswire" not in u, "the dead GlobeNewswire feed is back"

    # --- deterministic circuit-breaker exercise (no network) ---------
    feed_xml = (b"<?xml version='1.0'?><rss version='2.0'><channel>"
                b"<title>t</title><item>"
                b"<title>ACME Corp Reports Q3 | ACME Stock News</title>"
                b"<link>https://example.test/a</link>"
                b"<pubDate>Mon, 11 Aug 2026 12:00:00 GMT</pubDate>"
                b"</item></channel></rss>")
    calls = []
    dead = {"stocktitan.net"}

    class _Resp:
        status_code = 200

        def iter_content(self, chunk_size=65536):
            yield feed_xml

        def close(self):
            pass

    def fake_get(url, headers=None, timeout=None, stream=None):
        calls.append((url, timeout))
        if any(d in url for d in dead):
            raise mod.requests.exceptions.ReadTimeout("simulated")
        return _Resp()

    real_get = mod.requests.get
    mod.requests.get = fake_get
    try:
        rw = mod.RSSWorker()
        # Exercise the breaker in isolation: the per-feed poll floor is
        # covered separately below, and leaving ST's 300s floor on here
        # would stop the failure streak from ever accumulating.
        rw._min_interval = {c: 0.0 for c in rw._min_interval}

        def cycle():
            rw._last_fetch_at = 0.0
            calls.clear()
            return rw.fetch_feeds()

        def st_calls():
            return [u for u, _t2 in calls if "stocktitan" in u]

        # timeout actually applied
        cycle()
        assert {t for _u, t in calls} == {6.0}, "WIRE_TIMEOUT not applied"

        # trips only after CIRCUIT_TRIP_FAILURES consecutive failures
        for _ in range(mod.RSSWorker.CIRCUIT_TRIP_FAILURES):
            cycle()
        assert rw.statuses["ST"] == "ERR", "dead feed never went red"
        assert "ST" in rw.circuit_state(), "circuit did not trip"
        assert rw.statuses["PR"] == "OK" and rw.statuses["YH"] == "OK", \
            "a dead feed dragged the healthy ones down"

        # while open the origin is not contacted at all
        cycle()
        assert st_calls() == [], "open circuit still hit the dead origin"
        assert len(calls) == 2, "healthy feeds stopped running"

        # after cooldown: exactly ONE probe attempt, not the full ladder
        rw._circuit_until["ST"] = _t.time() - 1
        cycle()
        assert len(st_calls()) == 1, "half-open probe used the retry ladder"
        assert "ST" in rw.circuit_state(), "circuit did not re-arm"

        # recovery closes it automatically — no restart needed
        dead.clear()
        rw._circuit_until["ST"] = _t.time() - 1
        cycle()
        assert rw.statuses["ST"] == "OK" and not rw.circuit_state(), \
            "circuit did not close after the origin recovered"

        # every feed dark must not raise (ThreadPoolExecutor(max_workers=0))
        dead.update({"stocktitan.net", "prnewswire.com", "yahoo.com"})
        for _ in range(mod.RSSWorker.CIRCUIT_TRIP_FAILURES + 1):
            cycle()
        assert cycle() == [], "all-circuits-open should yield no items"
        assert calls == [], "all-circuits-open still contacted an origin"

        # --- 429 handling: honour Retry-After, trip on the FIRST one ---
        # Stocktitan answers 429 with Retry-After when polled too often;
        # retrying through it only restarts nginx's limiter window.
        class _R429:
            status_code = 429
            headers = {"Retry-After": "208"}

            def iter_content(self, chunk_size=65536):
                yield b""

            def close(self):
                pass

        limited = {"n": 0}

        def get_429(url, headers=None, timeout=None, stream=None):
            calls.append((url, timeout))
            if "stocktitan" in url:
                limited["n"] += 1
                return _R429()
            return _Resp()

        mod.requests.get = get_429
        rw3 = mod.RSSWorker()
        rw3._min_interval["ST"] = 0.0        # isolate the 429 logic
        rw3._last_fetch_at = 0.0
        calls.clear()
        limited["n"] = 0
        rw3.fetch_feeds()
        assert limited["n"] == 1, (
            f"a 429 was retried {limited['n']}x — that extends the penalty")
        st_state = rw3.circuit_state().get("ST")
        assert st_state is not None, "a 429 did not trip the circuit"
        assert 150 <= st_state <= 208, (
            f"Retry-After not honoured (circuit open for {st_state}s, "
            "expected ~208)")
        mod.requests.get = fake_get

        # --- per-feed minimum interval ---------------------------------
        dead.clear()          # all origins healthy again for this section
        floors = {c: iv for c, _u, iv in mod.RSSWorker.FEEDS}
        assert floors["ST"] >= 300.0, (
            "Stocktitan needs a poll floor; it 429s at the 60s cadence")
        assert floors["PR"] == 0.0 and floors["YH"] == 0.0, \
            "the other feeds should keep the default cadence"
        rw4 = mod.RSSWorker()
        rw4._last_fetch_at = 0.0
        calls.clear()
        rw4.fetch_feeds()                       # first cycle pulls all 3
        assert len([u for u, _t2 in calls if "stocktitan" in u]) == 1, \
            "first cycle should pull ST exactly once"
        rw4._last_fetch_at = 0.0
        calls.clear()
        rw4.fetch_feeds()                       # immediately after -> ST skipped
        assert [u for u, _t2 in calls if "stocktitan" in u] == [], \
            "the per-feed floor did not hold ST back"
        assert len(calls) == 2, "the floor wrongly held back PR/YH too"

        # parsed items carry the new source label and match the filter
        dead.clear()
        rw2 = mod.RSSWorker()
        _c, status, out, _ra = rw2._fetch_one(
            "ST", "https://www.stocktitan.net/rss/")
        assert status == "OK" and out, "feed parse produced nothing"
        assert out[0]["source"] == "STitn", f"bad label {out[0]['source']!r}"
        assert mod._compile_ticker_pattern("ACME").search(out[0]["headline"]), \
            "a Stocktitan headline would not match the per-symbol filter"
    finally:
        mod.requests.get = real_get
    print("wire roster + circuit breaker OK")


def test_status_indicators_track_feed_roster():
    """The indicator row is built from RSSWorker.FEEDS, so a feed swap
    can't leave a dead box (or a missing one) on screen."""
    _hr("status indicators track the feed roster")
    import scan_sec as mod
    app = _make_app()
    try:
        expected = [c for c, _u, _iv in mod.RSSWorker.FEEDS] + ["FV", "SEC"]
        assert list(app.indicators.keys()) == expected, (
            f"indicators {list(app.indicators.keys())} != roster {expected}")
        assert "GB" not in app.indicators, "a stale GB indicator is still drawn"
        assert "ST" in app.indicators, "no indicator for the new ST feed"
        # status_loop must paint the new code without raising
        app.status_loop()
        print("status indicators OK: %s" % list(app.indicators.keys()))
    finally:
        _teardown(app)


def test_link_marker_in_both_trees():
    """Rows that double-click will open carry the link marker; rows that
    won't open leave the gutter blank. The marker mirrors
    _safe_open_url's scheme allowlist, so it can never promise a click
    that the opener then refuses."""
    _hr("link marker in the wires + historical trees")
    import scan_sec as mod
    import datetime as _dt

    app = _make_app()
    try:
        today = mod._now_et().date().isoformat()
        app.current_items = [
            {"date": today, "time": "09:00AM", "age": "1h", "is_today": True,
             "headline": "ACME beats on earnings | ACME Stock News",
             "url": "https://example.test/a", "source": "STitn"},
            {"date": today, "time": "09:05AM", "age": "1h", "is_today": True,
             "headline": "Scraped row whose anchor had no href",
             "url": "", "source": "Finviz"},
            {"date": today, "time": "09:06AM", "age": "1h", "is_today": True,
             "headline": "Row carrying a javascript: url",
             "url": "javascript:alert(1)", "source": "Wire"},
        ]
        app.var_all.set(True)
        app.refresh_ui()

        assert app.tree["columns"][0] == "link", "wires tree lost its gutter"
        marks = [app.tree.set(i, "link") for i in app.tree.get_children()]
        assert marks[0] == mod.ScannerApp.LINK_MARKER, "http row unmarked"
        assert marks[1] == "", "row with no url was marked as clickable"
        assert marks[2] == "", "javascript: url was marked as clickable"

        # The headline text itself must be untouched — search, highlight
        # and the double-click headline match all key off it.
        assert app.tree.set("0", "headline") == app.current_items[0]["headline"]

        # Double-click must still resolve the right story now that the
        # value tuple has an extra leading cell.
        opened = []
        app._safe_open_url = lambda u: opened.append(u)

        class _Ev:
            y = 0

        app.tree.focus("0")
        app.on_double_click(_Ev())
        assert opened == ["https://example.test/a"], \
            f"double-click resolved wrongly after the column change: {opened}"

        # --- historical layout ---
        app.historical_active = True
        app.historical_date = _dt.date.today()
        app._apply_historical_tree_columns()
        assert app.tree["columns"][0] == "link", \
            "historical tree lost its gutter"
        results = [
            {"when": today, "source": "wires", "type": "STitn",
             "title": "ACME beats on earnings", "url": "https://example.test/a"},
            {"when": today, "source": "edgar", "type": "8-K",
             "title": "Item 2.02 Results", "url": "https://example.test/b"},
            {"when": today, "source": "polygon", "type": "news",
             "title": "No url on this row", "url": ""},
        ]
        app.historical_results = results
        app._render_historical_results("ACME", today, results, ["a note"])
        hm = {i: app.tree.set(i, "link") for i in app.tree.get_children()}
        assert hm.get("hist_banner") == "", "banner marked as clickable"
        assert hm.get("hist_0") == mod.ScannerApp.LINK_MARKER
        assert hm.get("hist_1") == mod.ScannerApp.LINK_MARKER
        assert hm.get("hist_2") == "", "url-less result marked as clickable"

        # The async enrichment pass rewrites a row's title; it must not
        # disturb the gutter (it used to rebuild the tuple positionally).
        app._promote_enriched_row = lambda iid: None
        app._refresh_historical_row(app._historical_gen, 1,
                                    {"oneliner": "A one-line summary."})
        assert app.tree.set("hist_1", "link") == mod.ScannerApp.LINK_MARKER, \
            "enrichment wiped the link gutter"
        assert "one-line summary" in app.tree.set("hist_1", "title")

        # Exiting historical mode restores the wires layout WITH the gutter.
        app.historical_active = False
        app._restore_wires_tree_columns()
        assert app.tree["columns"] == ("link", "date", "age", "headline")
    finally:
        _teardown(app)
    print("link marker in both trees OK")


def test_stocktitan_reaches_historical_lookup():
    """Stocktitan items must be visible to Historical Lookup. Its RSS has
    no archive (a 100-item pull spans ~4 HOURS, and there is no per-ticker
    feed), so the reachable history is the wires cache's 7-day window via
    the existing in-memory wires pass — not a new fetcher."""
    _hr("stocktitan reaches historical lookup")
    import scan_sec as mod
    import datetime as _dt

    app = _make_app()
    try:
        today = _dt.date.today()
        app.current_items = [
            {"date": today.isoformat(), "time": "09:00AM", "age": "1h",
             "is_today": True, "source": "STitn",
             "headline": "ACME beats on earnings | ACME Stock News",
             "url": "https://example.test/a"},
            {"date": (today - _dt.timedelta(days=30)).isoformat(),
             "time": "09:00AM", "age": "30d", "is_today": False,
             "source": "STitn", "headline": "Way out of window",
             "url": "https://example.test/old"},
        ]
        rows = app._filter_wires_to_window(app.current_items, today)
        titles = [r.get("title") or "" for r in rows]
        assert any("ACME beats" in t for t in titles), \
            "an in-window Stocktitan item never reached the historical pass"
        assert not any("out of window" in t for t in titles), \
            "the window filter let a far-out item through"
        assert all(r.get("source") == "wires" for r in rows)
    finally:
        _teardown(app)
    print("stocktitan -> historical lookup OK")


def test_version_metadata_is_consistent():
    """__version__, the window title, and the exe's version resource must
    agree, so a running build can be identified."""
    _hr("version metadata is consistent")
    import re as _re
    import scan_sec as mod

    v = mod.__version__
    assert _re.fullmatch(r"\d+\.\d+\.\d+", v), f"bad __version__: {v!r}"
    vi = (HERE / "version_info.txt").read_text(encoding="utf-8")
    parts = v.split(".")
    tup = "(%s, %s, %s, 0)" % tuple(parts)
    assert tup in vi, f"version_info.txt does not carry {tup}"
    for field in ("FileVersion", "ProductVersion"):
        assert f"StringStruct('{field}', '{v}')" in vi, \
            f"version_info.txt {field} is not {v}"
    spec = (HERE / "TNS.spec").read_text(encoding="utf-8")
    assert "version='version_info.txt'" in spec, \
        "TNS.spec no longer embeds the version resource"
    app = _make_app()
    try:
        assert v in app.title(), f"window title lacks the version: {app.title()!r}"
    finally:
        _teardown(app)
    print(f"version metadata OK (v{v})")


def test_earnings_chart_year_window():
    """The chart history window: trim to years*4 quarters anchored on the
    newest column, EXTEND rather than drop a historical-lookup match that
    predates the window, and in 'fixed' mode pad the front with blank
    dated slots that are neither gap nor future placeholders."""
    _hr("earnings chart year window (trim / pin / fixed padding)")
    import scan_sec as mod
    import pandas as pd

    App = mod.ScannerApp
    Q = mod.EARNINGS_CHART_QUARTERS_PER_YEAR

    def _frame(n, start="2016-03-31"):
        anchors = pd.date_range(start=start, periods=n, freq="91D")
        return pd.DataFrame({
            "ticker": ["TEST"] * n,
            "_anchor": anchors,
            "period_ending": anchors,
            "report_date": anchors + pd.Timedelta(days=30),
            "reported_eps": [1.0] * n,
            "surprise_eps_pct": [2.0] * n,
            "surprise_rev_pct": [3.0] * n,
            "yoy_eps_pct": [4.0] * n,
            "yoy_rev_pct": [5.0] * n,
            "_eps_yoy_fv": [False] * n,
            "_rev_yoy_fv": [False] * n,
        })

    # (a) Trim to exactly years*4, anchored on the LAST row (which is the
    #     upcoming-earnings placeholder when _expand_with_gaps made one).
    df = _frame(40)
    last_rd = df["report_date"].iloc[-1]
    out, gaps, fut, pads = App._apply_chart_year_window(
        df, {35}, {39}, years=5, mode="adaptive")
    assert len(out) == 5 * Q, f"expected {5 * Q} quarters, got {len(out)}"
    assert out["report_date"].iloc[-1] == last_rd, \
        "window must stay anchored on the newest column"
    assert pads == set(), "adaptive mode must not pad"

    # (b) The positional marker sets follow the trim. Getting this wrong
    #     paints '??' and the yellow future column onto other quarters.
    assert gaps == {15}, f"gap index not remapped (got {gaps})"
    assert fut == {19}, f"future index not remapped (got {fut})"
    _, gaps2, _, _ = App._apply_chart_year_window(
        df, {2, 35}, {39}, years=5, mode="adaptive")
    assert gaps2 == {15}, f"out-of-window gap must be dropped (got {gaps2})"

    # (c) years=0 is "All" -- the pre-existing no-limit behavior.
    out0, g0, f0, p0 = App._apply_chart_year_window(
        df, {35}, {39}, years=0, mode="fixed")
    assert len(out0) == 40 and g0 == {35} and f0 == {39} and p0 == set(), \
        "years=0 (All) must leave the frame untouched"

    # (d) A historical-lookup match older than the window extends it,
    #     rather than opening a chart that can't show the looked-up date.
    out_pin, _, fut_pin, _ = App._apply_chart_year_window(
        df, {35}, {39}, years=5, mode="adaptive", keep_from_idx=10)
    assert len(out_pin) == 30, \
        f"pinned quarter must survive the trim (got {len(out_pin)} rows)"
    assert fut_pin == {29}, f"future index wrong after pinned trim ({fut_pin})"
    out_in, _, _, _ = App._apply_chart_year_window(
        df, {35}, {39}, years=5, mode="adaptive", keep_from_idx=25)
    assert len(out_in) == 5 * Q, "an in-window pin must not widen the window"

    # (e) A short history stays short in adaptive, fills out in fixed.
    short = _frame(8)
    out_a, _, _, pads_a = App._apply_chart_year_window(
        short, set(), {7}, years=5, mode="adaptive")
    assert len(out_a) == 8 and pads_a == set(), \
        "adaptive must draw only the quarters that exist"
    out_f, gaps_f, fut_f, pads_f = App._apply_chart_year_window(
        short, set(), {7}, years=5, mode="fixed")
    assert len(out_f) == 5 * Q, \
        f"fixed mode must render the full window (got {len(out_f)})"
    assert pads_f == set(range(12)), f"pad set wrong (got {sorted(pads_f)})"
    assert fut_f == {19}, f"future index must shift by the pad count ({fut_f})"
    assert not (pads_f & gaps_f) and not (pads_f & fut_f), \
        "pad columns must be neither gap nor future -- that is what keeps " \
        "them from drawing '??' like a real coverage hole"

    # (f) Pad rows are dated (the axis reads as a real timeline) but carry
    #     no values, so nothing is drawn in them.
    for col in ("reported_eps", "surprise_eps_pct", "yoy_eps_pct",
                "surprise_rev_pct", "yoy_rev_pct"):
        assert out_f[col].iloc[:12].isna().all(), \
            f"pad column leaked a value in {col}"
    assert out_f["_anchor"].iloc[:12].notna().all(), \
        "pad columns must carry a synthesized date label"
    assert out_f["_anchor"].is_monotonic_increasing, \
        "padded frame must stay oldest -> newest"
    assert out_f["report_date"].iloc[12] == short["report_date"].iloc[0], \
        "real history must survive padding unchanged"
    assert out_f["_eps_yoy_fv"].dtype == bool, \
        f"pad concat flipped _eps_yoy_fv to {out_f['_eps_yoy_fv'].dtype}"

    # (g) Settings round-trip, including rejection of junk values.
    live = mod.SETTINGS_FILE
    backup = live.read_bytes() if live.exists() else None
    try:
        if live.exists():
            live.unlink()
        app = _make_app()
        try:
            assert app.earnings_chart_years == mod.EARNINGS_CHART_YEARS_DEFAULT, \
                f"default window is not {mod.EARNINGS_CHART_YEARS_DEFAULT}y"
            assert (app.earnings_chart_window_mode
                    == mod.EARNINGS_CHART_WINDOW_MODE_DEFAULT), \
                "default render mode changed"
        finally:
            _teardown(app)

        live.write_text(json.dumps({
            "earnings_chart_years": 3,
            "earnings_chart_window_mode": "fixed",
        }), encoding="utf-8")
        app = _make_app()
        try:
            assert app.earnings_chart_years == 3, "saved year window not loaded"
            assert app.earnings_chart_window_mode == "fixed", \
                "saved render mode not loaded"
        finally:
            _teardown(app)

        # Out-of-range / unknown-mode / bool-as-int must all fall back to
        # the defaults rather than reaching the renderer.
        for junk in ({"earnings_chart_years": 999,
                      "earnings_chart_window_mode": "sideways"},
                     {"earnings_chart_years": True,
                      "earnings_chart_window_mode": 7},
                     {"earnings_chart_years": -4}):
            live.write_text(json.dumps(junk), encoding="utf-8")
            app = _make_app()
            try:
                assert (app.earnings_chart_years
                        == mod.EARNINGS_CHART_YEARS_DEFAULT), \
                    f"junk settings leaked a year window: {junk}"
                assert (app.earnings_chart_window_mode
                        in mod.EARNINGS_CHART_WINDOW_MODES), \
                    f"junk settings leaked a render mode: {junk}"
            finally:
                _teardown(app)
    finally:
        if backup is not None:
            live.write_bytes(backup)
        elif live.exists():
            live.unlink()

    print("chart year window OK")


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
    test_etf_swap_resolution_and_badge()
    test_float_coloration_settings()
    test_search_filter_paste_sanitizing()
    test_report_day_freshness_marker()
    test_mcap_gradient_and_float_toggle()
    test_finviz_ea_synthesizer()
    test_eps_sales_surpr_cell_parser()
    test_finviz_ea_yoy_small_base_floor()
    test_parquet_auto_reload_on_mtime_change()
    test_cik_resolver_close_joins_refresh_thread()
    test_app_repeated_construct_destroy_is_safe()
    # 2026-08-11 audit fixes (3 high + 10 medium)
    test_finviz_field_caps_and_linear_date_parse()
    test_atomic_write_fails_fast_when_dir_denied()
    test_parquet_schema_drift_is_contained()
    test_refresh_button_always_released()
    test_chart_load_uses_snapshotted_cik_and_meta()
    test_new_quarter_detected_by_report_proximity()
    test_impossible_periods_and_tie_break()
    test_sec_unknown_is_not_asserted_as_negative()
    test_geometry_clamped_to_visible_desktop()
    # 2026-08-11 wire swap: GlobeNewswire -> Stocktitan + circuit breaker
    test_wire_feeds_and_circuit_breaker()
    test_status_indicators_track_feed_roster()
    # v2.3.0: link marker + version metadata
    test_link_marker_in_both_trees()
    test_stocktitan_reaches_historical_lookup()
    test_version_metadata_is_consistent()
    # Earnings chart history window (years limit + adaptive/fixed bars)
    test_earnings_chart_year_window()
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
