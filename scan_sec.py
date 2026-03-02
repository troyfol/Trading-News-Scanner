# ==============================================================================
# Morning Scanner (Ultimate Edition) - Clickable Company Name (Finviz Link)
# Produced by troyfol
# ==============================================================================

import sys
import os
import time
import json
import threading
import tkinter as tk
from tkinter import ttk, font
from datetime import datetime, timedelta
from pathlib import Path
import re
import requests
from bs4 import BeautifulSoup
import difflib
import concurrent.futures
import webbrowser
from urllib.parse import quote as url_quote

try:
    import win32gui
except ImportError:
    print("ERROR: pywin32 is required. Install with: pip install pywin32")
    try:
        import tkinter.messagebox as _mb
        _r = tk.Tk(); _r.withdraw()
        _mb.showerror("Missing Dependency", "pywin32 is required.\nInstall with: pip install pywin32")
        _r.destroy()
    except Exception:
        pass
    sys.exit(1)

# --- CONFIG ---
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

WIRE_CACHE_PATH = BASE_DIR / "wires_cache.json"
SEC_CACHE_PATH = BASE_DIR / "sec_tickers.json" 
SETTINGS_FILE = BASE_DIR / "scanner_settings.json"

UA_LIST = ["MorningScanner/1.0 (individual_trader_research; +http://www.google.com)"]
HEADERS = {"User-Agent": UA_LIST[0]}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://finviz.com"
}
MIN_SCRAPE_INTERVAL = 2.2 

# --- THEMES ---
THEMES = {
    "dark": {
        "BG": "#141211", "FG": "#F2EFED", "ACCENT": "#2B2B2B",
        "TXT_OK": "#00FF00", "TXT_BAD": "#FF4444", "HIGHLIGHT": "#00D7FF", 
        "CREDIT": "#888888", "VIOLET": "#D699FF", "STATUS_WAIT": "#555555", 
        "STATUS_OK": "#00FF00", "STATUS_ERR": "#FF4444",
        "ENTRY_BG": "#333333", "ENTRY_FG": "white", "BTN_BG": "#444444", "BTN_FG": "white",
        "TREE_SEL": "#444444", "TREE_OLD": "#888888",
        "SEC_HOT": "#00FF00", "SEC_WARM": "#66FF66", "SEC_COLD": "#888888"
    },
    "light": {
        "BG": "#FFFFFF", "FG": "#000000", "ACCENT": "#E0E0E0",
        "TXT_OK": "#00AA00", "TXT_BAD": "#CC0000", "HIGHLIGHT": "#0000FF", 
        "CREDIT": "#888888", "VIOLET": "#8A2BE2", "STATUS_WAIT": "#CCCCCC", 
        "STATUS_OK": "#00FF00", "STATUS_ERR": "#FF4444",
        "ENTRY_BG": "#EEEEEE", "ENTRY_FG": "black", "BTN_BG": "#DDDDDD", "BTN_FG": "black",
        "TREE_SEL": "#CCCCCC", "TREE_OLD": "#888888",
        "SEC_HOT": "#00AA00", "SEC_WARM": "#66CC66", "SEC_COLD": "#888888"
    }
}

# ==============================================================================
# CIK RESOLVER
# ==============================================================================
class CIKResolver:
    def __init__(self):
        self.ticker_map = {}
        self.name_map = {}
        self._prefix_map = {}
        self.loaded = False
        self.load_local_cache()
        threading.Thread(target=self.refresh_sec_list, daemon=True).start()

    def normalize_name(self, name):
        if not name: return ""
        n = name.upper()
        n = re.sub(r'[^A-Z0-9\s]', '', n) 
        remove_words = ["INC", "CORP", "CORPORATION", "LTD", "LIMITED", "LLC", "COMPANY", "PLC"]
        tokens = [t for t in n.split() if t not in remove_words]
        return " ".join(tokens)

    def load_local_cache(self):
        if SEC_CACHE_PATH.exists():
            try:
                with open(SEC_CACHE_PATH, "r") as f:
                    data = json.load(f)
                    self.process_data(data)
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                pass

    def refresh_sec_list(self):
        url = "https://www.sec.gov/files/company_tickers.json"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                self.process_data(data)
                with open(SEC_CACHE_PATH, "w") as f:
                    json.dump(data, f)
        except (requests.RequestException, json.JSONDecodeError, OSError, KeyError, ValueError):
            pass

    def process_data(self, raw_json):
        t_map = {}
        n_map = {}
        prefix_map = {}  # first char -> list of (norm_name, cik)
        for k, v in raw_json.items():
            cik = str(v.get("cik_str")).zfill(10)
            tick = v.get("ticker", "").upper()
            title = v.get("title", "")
            t_map[tick] = {"cik": cik, "title": title}
            norm_title = self.normalize_name(title)
            if norm_title:
                n_map[norm_title] = cik
                ch = norm_title[0]
                if ch not in prefix_map:
                    prefix_map[ch] = []
                prefix_map[ch].append((norm_title, cik))
        self.ticker_map = t_map
        self.name_map = n_map
        self._prefix_map = prefix_map
        self.loaded = True

    def get_cik(self, symbol, window_title_name=None):
        clean_sym = symbol.upper().strip()
        found = self.ticker_map.get(clean_sym)
        if found:
            if window_title_name:
                sec_name = found['title']
                norm_sec = self.normalize_name(sec_name)
                norm_win = self.normalize_name(window_title_name)
                sec_words = norm_sec.split()
                win_words = norm_win.split()
                if sec_words and win_words:
                    matcher = difflib.SequenceMatcher(None, norm_sec, norm_win)
                    if matcher.ratio() > 0.4: return found['cik']
                    else: pass 
                else: return found['cik']
        if window_title_name:
            norm_win = self.normalize_name(window_title_name)
            if norm_win in self.name_map: return self.name_map[norm_win]
            best_match = None
            best_ratio = 0.0
            candidates = self._prefix_map.get(norm_win[0], []) if norm_win else []
            for sec_name, cik in candidates:
                ratio = difflib.SequenceMatcher(None, sec_name, norm_win).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = cik
            if best_ratio > 0.6: return best_match
        if found: return found['cik']
        return symbol

# ==============================================================================
# INTERNAL RSS WORKER
# ==============================================================================
class RSSWorker:
    def __init__(self):
        self.running = False
        self.feeds = [
            ("GB", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
            ("PR", "https://www.prnewswire.com/rss/news-releases-list.rss"),
            ("YH", "https://finance.yahoo.com/news/rssindex")
        ]
        self.statuses = {code: None for code, url in self.feeds}
    
    def fetch_feeds(self):
        items = []
        for code, url in self.feeds:
            try:
                source = "Wire"
                if code == "YH": source = "Yahoo"
                elif code == "PR": source = "PRNew"
                elif code == "GB": source = "Globe"
                r = requests.get(url, headers=BROWSER_HEADERS, timeout=10)
                if r.status_code == 200:
                    self.statuses[code] = "OK"
                    raw_items = re.findall(r'<item>(.*?)</item>', r.text, re.DOTALL)
                    for raw in raw_items:
                        title_m = re.search(r'<title>(.*?)</title>', raw, re.DOTALL)
                        link_m = re.search(r'<link>(.*?)</link>', raw, re.DOTALL)
                        title = ""
                        if title_m: title = title_m.group(1).replace("<![CDATA[", "").replace("]]>", "").strip()
                        link = ""
                        if link_m: link = link_m.group(1).strip()
                        if title:
                            t_str = datetime.now().strftime("%I:%M%p")
                            items.append({ "source": source, "headline": title, "url": link, "time": t_str, "tickers": [] })
                else: self.statuses[code] = "ERR"
            except (requests.RequestException, OSError):
                self.statuses[code] = "ERR"
        return items

    def load_cache(self):
        if WIRE_CACHE_PATH.exists():
            try:
                with open(WIRE_CACHE_PATH, "r", encoding="utf-8") as f: return json.load(f).get("items", [])
            except (json.JSONDecodeError, OSError, KeyError):
                pass
        return []

    def save_cache(self, items):
        try:
            temp = WIRE_CACHE_PATH.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as f: json.dump({"items": items}, f, indent=2)
            os.replace(temp, WIRE_CACHE_PATH)
        except (OSError, TypeError):
            pass

    def run_loop(self):
        self.running = True
        while self.running:
            try:
                cached_items = self.load_cache()
                new_items = self.fetch_feeds()
                seen_urls = set(i.get("url") for i in cached_items if i.get("url"))
                merged = cached_items.copy()
                count_new = 0
                for it in new_items:
                    if it.get("url") and it.get("url") not in seen_urls:
                        merged.insert(0, it)
                        seen_urls.add(it.get("url"))
                        count_new += 1
                # Prune items older than 7 days
                cutoff = (datetime.now() - timedelta(days=7)).isoformat()
                merged = [m for m in merged if m.get("date", "") >= cutoff or not m.get("date")]
                merged = merged[:500]
                if count_new > 0 or not cached_items: self.save_cache(merged)
            except (requests.RequestException, OSError, KeyError, TypeError, ValueError) as e:
                print(f"RSS loop error: {e}")
            time.sleep(60)

    def start(self):
        t = threading.Thread(target=self.run_loop, daemon=True)
        t.start()

# ==============================================================================
# DATA FETCHER
# ==============================================================================
class DataFetcher:
    def __init__(self):
        self.session = requests.Session()
        self._scrape_lock = threading.Lock()
        self.last_scrape_time = 0
        self.finviz_status = None
        self.sec_status = None
        self.rss_worker = RSSWorker()
        self.rss_worker.start()
        self.cik_resolver = CIKResolver()

    def close(self):
        self.rss_worker.running = False
        self.session.close()

    def get_time_ago(self, date_obj, time_str):
        if not time_str or date_obj != datetime.now().date(): return ""
        try:
            now = datetime.now()
            dt_time = datetime.strptime(time_str, "%I:%M%p").time()
            dt_news = datetime.combine(now.date(), dt_time)
            diff = (now - dt_news).total_seconds()
            if diff < 60: return "now"
            mins = int(diff / 60)
            if mins < 60: return f"{mins}m"
            return f"{int(mins/60)}h"
        except (ValueError, TypeError, AttributeError):
            return ""

    def parse_float(self, text):
        if not text: return text, False
        clean = text.upper().strip()
        try:
            val = 0.0
            if clean.endswith("M"): val = float(clean[:-1]) * 1_000_000
            elif clean.endswith("B"): val = float(clean[:-1]) * 1_000_000_000
            elif clean.endswith("K"): val = float(clean[:-1]) * 1_000
            else: val = float(clean)
            return text, (val < 20_000_000)
        except (ValueError, TypeError):
            return text, False

    def get_wires(self, symbol):
        items = []
        if not WIRE_CACHE_PATH.exists(): return items
        try:
            with open(WIRE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_list = data.get("items", [])
            today_iso = datetime.now().date().isoformat()
            for w in raw_list:
                head = w.get("headline", "")
                if re.search(rf"\b{re.escape(symbol)}\b", head, re.IGNORECASE):
                    t_str = w.get("time", "")
                    items.append({
                        "date": today_iso, "time": t_str, "age": self.get_time_ago(datetime.now().date(), t_str),
                        "headline": head, "url": w.get("url", ""), "source": w.get("source", "Wire"), "is_today": True
                    })
        except (json.JSONDecodeError, OSError, KeyError):
            pass
        return items

    def scrape_sec_shelf(self, symbol, resolved_cik):
        query_val = resolved_cik if resolved_cik else symbol
        self.sec_status = None
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={query_val}&type=S-3&dateb=&owner=exclude&count=10"
        try:
            r = self.session.get(url, headers=HEADERS, timeout=8)
            if r.status_code == 200: self.sec_status = "OK"
            else:
                self.sec_status = "ERR"
                return False
            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.find("table", class_="tableFile2")
            if not table: return False
            for row in table.find_all("tr")[1:]: 
                cols = row.find_all("td")
                if len(cols) >= 4:
                    filing_type = cols[0].get_text(strip=True)
                    date_str = cols[3].get_text(strip=True)
                    if "S-3" in filing_type:
                        try:
                            f_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                            if (datetime.now().date() - f_date).days < 1095: return True
                        except (ValueError, TypeError):
                            pass
            return False
        except (requests.RequestException, OSError):
            self.sec_status = "ERR"
            return False

    def scrape_sec_recent(self, symbol, resolved_cik):
        query_val = resolved_cik if resolved_cik else symbol
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={query_val}&count=10"
        try:
            r = self.session.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200: return 2
            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.find("table", class_="tableFile2")
            if not table: return 2
            rows = table.find_all("tr")
            if len(rows) < 2: return 2
            cols = rows[1].find_all("td")
            if len(cols) < 4: return 2
            date_str = cols[3].get_text(strip=True)
            f_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            today = datetime.now().date()
            diff = (today - f_date).days
            if diff == 0: return 0
            if diff <= 1: return 1
            return 2
        except (requests.RequestException, OSError, ValueError, AttributeError):
            return 2

    def scrape_finviz(self, symbol):
        self.finviz_status = None
        with self._scrape_lock:
            now = time.time()
            if now - self.last_scrape_time < MIN_SCRAPE_INTERVAL:
                time.sleep(MIN_SCRAPE_INTERVAL - (now - self.last_scrape_time))
            self.last_scrape_time = time.time()
        
        url = f"https://finviz.com/quote.ashx?t={symbol}&p=d"
        meta = {"name": "", "catalyst": "", "float": "", "short": "", "sector": "", "country": "", "mcap": "", "rvol": "", "is_low": False}
        items = []
        try:
            r = self.session.get(url, headers=BROWSER_HEADERS, timeout=5)
            if r.status_code == 200: self.finviz_status = "OK"
            else: self.finviz_status = "ERR"
                
            soup = BeautifulSoup(r.text, "html.parser")
            
            try:
                title_text = soup.find("title").get_text(strip=True)
                if "-" in title_text:
                    parts = title_text.split("-", 1)[1]
                    name_clean = parts.split("Stock")[0].strip()
                    meta["name"] = name_clean
            except (AttributeError, TypeError):
                pass

            try:
                catalyst_node = soup.find(string=re.compile(r"Today,\s+\d{1,2}:\d{2}\s*[AP]M"))
                if catalyst_node:
                    container = catalyst_node.parent
                    full_text = container.get_text(strip=True, separator=" ")
                    if len(full_text) < 25:
                        full_text = container.parent.get_text(strip=True, separator=" ")
                    meta["catalyst"] = full_text
            except (AttributeError, TypeError):
                pass

            for a in soup.find_all("a", href=True):
                if "f=sec_" in a["href"]: meta["sector"] = a.get_text(strip=True)
                elif "f=geo_" in a["href"]: meta["country"] = a.get_text(strip=True)
                if meta["sector"] and meta["country"]: break
            
            snap = soup.find("table", class_="snapshot-table2")
            if snap:
                for tr in snap.find_all("tr"):
                    tds = tr.find_all("td")
                    for i in range(0, len(tds)-1):
                        txt = tds[i].get_text(strip=True).lower()
                        val = tds[i+1].get_text(strip=True)
                        if "shs float" in txt: meta["float"], meta["is_low"] = self.parse_float(val)
                        elif "short float" in txt: meta["short"] = val
                        elif "market cap" in txt: meta["mcap"] = val
                        elif "rel volume" in txt: meta["rvol"] = val

            news_table = soup.find(id="news-table")
            if news_table:
                today_date = datetime.now().date()
                curr_date = today_date
                for tr in news_table.find_all("tr"):
                    tds = tr.find_all("td")
                    if len(tds) < 2: continue
                    ts_txt = tds[0].get_text(strip=True)
                    time_part = ""
                    if "Today" in ts_txt: curr_date = today_date
                    elif "Yesterday" in ts_txt: curr_date = today_date - timedelta(days=1)
                    elif "-" in ts_txt:
                        try: curr_date = datetime.strptime(ts_txt.split()[0], "%b-%d-%y").date()
                        except (ValueError, IndexError): pass
                    if ts_txt.endswith("M"): time_part = ts_txt.split()[-1]
                    link = tds[1].find("a")
                    headline = link.get_text(strip=True) if link else tds[1].get_text(strip=True)
                    items.append({
                        "date": curr_date.isoformat(), "time": time_part, "age": self.get_time_ago(curr_date, time_part),
                        "headline": headline, "url": link["href"] if link else "", "source": "Finviz", "is_today": (curr_date == today_date)
                    })
        except (requests.RequestException, OSError, AttributeError):
            self.finviz_status = "ERR"
        return meta, items

# ==============================================================================
# WINDOW WATCHER
# ==============================================================================
class WindowWatcher:
    def __init__(self):
        self.DEPTH_MARKERS = ["MARKET DEPTH", "MATRIX"]

    def _clean_symbol(self, token):
        s = re.sub(r"\(.*?\)", "", token)
        s = re.sub(r"\[.*?\]", "", s)
        s = re.sub(r"[^A-Z]", "", s)
        return s.strip()

    def _parse_depth_title(self, title):
        upper = title.upper().replace("–", "-").replace("—", "-")
        if not any(m in upper for m in self.DEPTH_MARKERS): return None, None
        parts = upper.split(" - ")
        found_sym = None
        found_name = None
        if len(parts) >= 2:
            raw_sym = parts[1].strip().split(" ")[0]
            found_sym = self._clean_symbol(raw_sym)
        if len(parts) >= 3:
            found_name = parts[2].strip()
        if not found_sym:
            m = re.search(r"([A-Z]+(?:\([A-Z]+\))?)\s*\[", upper)
            if m: found_sym = self._clean_symbol(m.group(1))
        if found_sym and 1 <= len(found_sym) <= 5:
            return found_sym, found_name
        return None, None

    def get_info(self):
        found_symbol = None
        found_name = None
        def child_enum_handler(hwnd, ctx):
            nonlocal found_symbol, found_name
            if found_symbol: return 0 
            title = win32gui.GetWindowText(hwnd)
            if any(m in title.upper() for m in self.DEPTH_MARKERS):
                sym, name = self._parse_depth_title(title)
                if sym: 
                    found_symbol = sym; found_name = name
                    return 0
            return 1
        def top_enum_handler(hwnd, ctx):
            if found_symbol: return
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if "TradeStation" in title:
                    try: win32gui.EnumChildWindows(hwnd, child_enum_handler, None)
                    except OSError: pass
        try: win32gui.EnumWindows(top_enum_handler, None)
        except OSError: pass
        return found_symbol, found_name

# ==============================================================================
# UI
# ==============================================================================
class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.fetcher = DataFetcher()
        self.watcher = WindowWatcher()
        self.current_symbol = None
        self.current_window_name = None
        self.current_cik = None
        self.current_meta = {}
        self.hot_words = []
        self.base_font_size = 10
        self.theme_mode = "dark"
        self.colors = THEMES[self.theme_mode]
        
        self.title("Morning Scanner")
        self.configure(bg=self.colors["BG"])
        self.attributes("-topmost", True)
        self.minsize(600, 420)
        self.load_settings()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.style = ttk.Style()
        self.style.theme_use("clam")
        
        self.hdr = tk.Frame(self, bg=self.colors["BG"])
        self.hdr.pack(fill="x", padx=10, pady=5)
        
        self.header_top = tk.Frame(self.hdr, bg=self.colors["BG"])
        self.header_top.pack(side="top", fill="x", anchor="w")

        self.lbl_symbol = tk.Label(self.header_top, text="—", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_symbol.pack(side="left", anchor="sw")
        
        self.lbl_float = tk.Label(self.header_top, text="", bg=self.colors["BG"], fg=self.colors["TXT_OK"])
        self.lbl_float.pack(side="left", padx=(15, 5), anchor="sw", pady=(0, 4))

        self.lbl_sec_recent = tk.Label(self.header_top, text="SEC: —", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_sec_recent.pack(side="left", padx=5, anchor="sw", pady=(0, 4))
        self.lbl_sec_recent.configure(cursor="hand2")
        self.lbl_sec_recent.bind("<Button-1>", self.open_sec_recent)

        self.lbl_shelf = tk.Label(self.header_top, text="Shelf: —", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_shelf.pack(side="left", padx=5, anchor="sw", pady=(0, 4))
        self.lbl_shelf.configure(cursor="hand2")
        self.lbl_shelf.bind("<Button-1>", self.open_sec_shelf_link)

        self.lbl_meta = tk.Label(self.header_top, text="", bg=self.colors["BG"], fg=self.colors["FG"])
        self.lbl_meta.pack(side="left", padx=5, anchor="sw", pady=(0, 4))

        # [CHANGE] Added cursor="hand2" and bound Button-1 for clickability
        self.lbl_name = tk.Label(self.hdr, text="", bg=self.colors["BG"], fg=self.colors["VIOLET"], anchor="w", justify="left", cursor="hand2")
        self.lbl_name.pack(side="top", anchor="w")
        self.lbl_name.bind("<Button-1>", self.open_finviz_link)

        self.ctrl = tk.Frame(self, bg=self.colors["BG"])
        self.ctrl.pack(fill="x", padx=10, pady=(0, 5))
        
        self.var_48 = tk.BooleanVar(value=False)
        self.chk_48 = tk.Checkbutton(self.ctrl, text="48h", variable=self.var_48, command=self.refresh_ui)
        self.chk_48.pack(side="left")

        self.var_all = tk.BooleanVar(value=False)
        self.chk_all = tk.Checkbutton(self.ctrl, text="All", variable=self.var_all, command=self.refresh_ui)
        self.chk_all.pack(side="left", padx=(5,0))
        
        self.var_mcap = tk.BooleanVar(value=False)
        self.chk_mcap = tk.Checkbutton(self.ctrl, text="MCap", variable=self.var_mcap, command=self.refresh_meta_label)
        self.chk_mcap.pack(side="left", padx=(5,0))
        
        self.var_rvol = tk.BooleanVar(value=False)
        self.chk_rvol = tk.Checkbutton(self.ctrl, text="Rel Vol", variable=self.var_rvol, command=self.refresh_meta_label)
        self.chk_rvol.pack(side="left")

        self.lbl_highlight = tk.Label(self.ctrl, text=" | Highlight:", bg=self.colors["BG"], fg="#888888")
        self.lbl_highlight.pack(side="left", padx=5)
        
        self.entry_hot = tk.Entry(self.ctrl, width=20)
        self.entry_hot.pack(side="left")
        self.entry_hot.bind("<Return>", self.apply_hot_words)
        
        self.btn_apply = tk.Button(self.ctrl, text="Apply", command=self.apply_hot_words, borderwidth=0, padx=10)
        self.btn_apply.pack(side="left", padx=5)

        self.btn_plus = tk.Button(self.ctrl, text="+", command=lambda: self.adjust_font(1), borderwidth=0, padx=8)
        self.btn_plus.pack(side="left", padx=(5,1))
        self.btn_minus = tk.Button(self.ctrl, text="-", command=lambda: self.adjust_font(-1), borderwidth=0, padx=8)
        self.btn_minus.pack(side="left", padx=1)
        
        self.btn_theme = tk.Button(self.ctrl, text="☀/☾", command=self.toggle_theme, borderwidth=0, padx=8)
        self.btn_theme.pack(side="left", padx=(5,1))

        self.stat_frame = tk.Frame(self.ctrl, bg=self.colors["BG"])
        self.stat_frame.pack(side="right", padx=10)
        self.indicators = {}
        self.status_widgets = {} 
        for i, code in enumerate(["PR", "GB", "YH", "FV", "SEC"]):
            f = tk.Frame(self.stat_frame, bg=self.colors["BG"])
            f.pack(side="left", padx=2)
            lbl = tk.Label(f, text=code, bg=self.colors["BG"], fg="#888888")
            lbl.pack(side="top")
            box = tk.Label(f, text="", width=2, height=1, bg=self.colors["STATUS_WAIT"])
            box.pack(side="bottom", pady=(1,0))
            self.indicators[code] = box
            self.status_widgets[code] = (lbl, f, box)

        self.tree = ttk.Treeview(self, columns=("date", "age", "headline"), show="headings", selectmode="browse")
        self.tree.heading("date", text="Date")
        self.tree.heading("age", text="Age")
        self.tree.heading("headline", text="Headline")
        self.tree.column("date", width=80, anchor="center")
        self.tree.column("age", width=70, anchor="center")
        self.tree.column("headline", width=500, anchor="w")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.tree.bind("<Double-1>", self.on_double_click)
        
        self.lbl_credit = tk.Label(self, text="Produced by troyfol", bg=self.colors["BG"], fg=self.colors["CREDIT"])
        self.lbl_credit.pack(side="bottom", pady=(0, 2))

        self.apply_theme() 
        self.adjust_font(0) 

        self.current_items = []
        self._displayed_indices = []
        self._fetch_gen = 0
        self.debounce_timer = None
        self.after(200, self.watch_loop)
        self.after(1000, self.status_loop)

    def toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        self.colors = THEMES[self.theme_mode]
        self.apply_theme()
        self.refresh_ui()

    def apply_theme(self):
        c = self.colors
        self.configure(bg=c["BG"])
        self.style.configure("Treeview", background=c["BG"], foreground=c["FG"], fieldbackground=c["BG"])
        self.style.configure("Treeview.Heading", background=c["ACCENT"], foreground=c["FG"])
        self.style.map("Treeview", background=[("selected", c["TREE_SEL"])], foreground=[("selected", c["FG"])])
        self.tree.tag_configure("hot", foreground=c["HIGHLIGHT"])
        self.tree.tag_configure("today", foreground=c["FG"])
        self.tree.tag_configure("old", foreground=c["TREE_OLD"])

        self.hdr.config(bg=c["BG"])
        self.header_top.config(bg=c["BG"])
        self.lbl_symbol.config(bg=c["BG"], fg=c["FG"])
        self.lbl_name.config(bg=c["BG"], fg=c["VIOLET"])
        self.lbl_shelf.config(bg=c["BG"])
        self.lbl_sec_recent.config(bg=c["BG"]) 
        self.lbl_float.config(bg=c["BG"]) 
        self.lbl_meta.config(bg=c["BG"], fg=c["FG"])
        
        self.ctrl.config(bg=c["BG"])
        cb_conf = {"bg": c["BG"], "fg": c["FG"], "selectcolor": c["BG"], "activebackground": c["BG"]}
        self.chk_48.config(**cb_conf)
        self.chk_all.config(**cb_conf)
        self.chk_mcap.config(**cb_conf)
        self.chk_rvol.config(**cb_conf)
        
        self.lbl_highlight.config(bg=c["BG"])
        self.entry_hot.config(bg=c["ENTRY_BG"], fg=c["ENTRY_FG"], insertbackground=c["ENTRY_FG"])
        
        btn_conf = {"bg": c["BTN_BG"], "fg": c["BTN_FG"]}
        self.btn_apply.config(**btn_conf)
        self.btn_plus.config(**btn_conf)
        self.btn_minus.config(**btn_conf)
        self.btn_theme.config(**btn_conf)
        
        self.stat_frame.config(bg=c["BG"])
        for code, (lbl, frame, box) in self.status_widgets.items():
            frame.config(bg=c["BG"])
            lbl.config(bg=c["BG"])
            
        self.lbl_credit.config(bg=c["BG"], fg=c["CREDIT"])

    def adjust_font(self, delta):
        self.base_font_size += delta
        if self.base_font_size < 7: self.base_font_size = 7
        if self.base_font_size > 20: self.base_font_size = 20
        s = self.base_font_size
        
        self.style.configure("Treeview", font=("Segoe UI", s), rowheight=int(s*2.4))
        self.style.configure("Treeview.Heading", font=("Segoe UI", s, "bold"))
        
        self.lbl_symbol.config(font=("Segoe UI", s+14, "bold"))
        self.lbl_name.config(font=("Segoe UI", max(6, s+1)))
        self.lbl_shelf.config(font=("Segoe UI", s+2, "bold"))
        self.lbl_sec_recent.config(font=("Segoe UI", s+2, "bold"))
        self.lbl_float.config(font=("Segoe UI", s+4, "bold"))
        self.lbl_meta.config(font=("Segoe UI", s+2))
        
        std = ("Segoe UI", s)
        self.chk_48.config(font=std); self.chk_all.config(font=std)
        self.chk_mcap.config(font=std); self.chk_rvol.config(font=std)
        self.lbl_highlight.config(font=std); self.entry_hot.config(font=std)
        self.btn_apply.config(font=std); self.btn_plus.config(font=std); self.btn_minus.config(font=std); self.btn_theme.config(font=std)
        
        tiny = ("Segoe UI", max(5, s-4))
        small_bold = ("Segoe UI", max(6, s-3), "bold")
        self.lbl_credit.config(font=("Segoe UI", max(6, s-2)))
        
        for code, (lbl, f, box) in self.status_widgets.items():
            lbl.config(font=small_bold); box.config(font=tiny)

    def watch_loop(self):
        sym, win_name = self.watcher.get_info()
        if (sym and sym != self.current_symbol):
            if self.debounce_timer: self.after_cancel(self.debounce_timer)
            self.debounce_timer = self.after(150, lambda: self.change_symbol(sym, win_name))
        self.after(500, self.watch_loop)

    def status_loop(self):
        rss_stats = self.fetcher.rss_worker.statuses
        c = self.colors
        for code in ["PR", "GB", "YH"]:
            s = rss_stats.get(code)
            color = c["STATUS_WAIT"]
            if s == "OK": color = c["STATUS_OK"]
            elif s == "ERR": color = c["STATUS_ERR"]
            self.indicators[code].config(bg=color)
        
        fv_stat = self.fetcher.finviz_status
        f_color = c["STATUS_WAIT"]
        if fv_stat == "OK": f_color = c["STATUS_OK"]
        elif fv_stat == "ERR": f_color = c["STATUS_ERR"]
        self.indicators["FV"].config(bg=f_color)
        
        sec_stat = self.fetcher.sec_status
        s_color = c["STATUS_WAIT"]
        if sec_stat == "OK": s_color = c["STATUS_OK"]
        elif sec_stat == "ERR": s_color = c["STATUS_ERR"]
        self.indicators["SEC"].config(bg=s_color)

        self.after(1000, self.status_loop)

    def change_symbol(self, sym, win_name):
        self._fetch_gen += 1
        self.current_symbol = sym
        self.current_window_name = win_name
        self.lbl_symbol.config(text=sym)
        self.lbl_name.config(text=win_name if win_name else "")
        self.lbl_shelf.config(text="Shelf: —", fg=self.colors["CREDIT"])
        self.lbl_sec_recent.config(text="SEC: —", fg=self.colors["FG"])
        self.lbl_float.config(text="")
        self.lbl_meta.config(text="Loading...")
        self.tree.delete(*self.tree.get_children())
        self.current_cik = self.fetcher.cik_resolver.get_cik(sym, win_name)
        wires = self.fetcher.get_wires(sym)
        self.current_items = wires
        self.refresh_ui()
        gen = self._fetch_gen
        threading.Thread(target=self.bg_fetch, args=(sym, gen), daemon=True).start()

    def bg_fetch(self, sym, gen):
        if gen != self._fetch_gen: return
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_meta = executor.submit(self.fetcher.scrape_finviz, sym)
            future_shelf = executor.submit(self.fetcher.scrape_sec_shelf, sym, self.current_cik)
            future_recent = executor.submit(self.fetcher.scrape_sec_recent, sym, self.current_cik)

            meta, fv_items = future_meta.result()
            has_s3 = future_shelf.result()
            sec_recent_status = future_recent.result()

        if gen != self._fetch_gen: return
        self.after(0, lambda: self.update_full_data(sym, meta, fv_items, has_s3, sec_recent_status))

    def update_full_data(self, sym, meta, fv_items, has_s3, sec_recent_status):
        if sym != self.current_symbol: return
        
        wires = self.fetcher.get_wires(sym)
        display_name = ""
        if meta.get("name"): display_name = meta["name"]
        elif self.current_window_name: display_name = self.current_window_name
            
        if meta.get("catalyst"):
             if display_name: display_name += f" - {meta['catalyst']}"
             else: display_name = meta['catalyst']

        self.lbl_name.config(text=display_name)

        if has_s3: self.lbl_shelf.config(text="Shelf: YES", fg=self.colors["FG"]) 
        else: self.lbl_shelf.config(text="Shelf: NO", fg=self.colors["CREDIT"]) 

        if sec_recent_status == 0: self.lbl_sec_recent.config(text="SEC: <24h", fg=self.colors["SEC_HOT"])
        elif sec_recent_status == 1: self.lbl_sec_recent.config(text="SEC: <48h", fg=self.colors["SEC_WARM"])
        else: self.lbl_sec_recent.config(text="SEC: >48h", fg=self.colors["SEC_COLD"])

        seen_urls = set(); seen_headlines = set(); merged = []
        for i in wires + fv_items:
            clean_head = i['headline'].strip().lower()
            u = i.get('url')
            if u and u in seen_urls: continue
            if clean_head in seen_headlines: continue
            if u: seen_urls.add(u)
            seen_headlines.add(clean_head)
            merged.append(i)
        self.current_items = merged
        self.current_meta = meta

        if meta.get("float"):
            fg_col = self.colors["TXT_OK"] if meta['is_low'] else self.colors["TXT_BAD"]
            self.lbl_float.config(text=f"Float {meta['float']}", fg=fg_col)
        
        self.refresh_meta_label()
        self.refresh_ui()

    def refresh_meta_label(self):
        m_txt = []
        meta = self.current_meta
        if meta.get("short"): m_txt.append(f"Short {meta['short']}")
        if self.var_mcap.get() and meta.get("mcap"): m_txt.append(f"MCap {meta['mcap']}")
        if self.var_rvol.get() and meta.get("rvol"): m_txt.append(f"RVol {meta['rvol']}")
        if meta.get("sector"): m_txt.append(f"{meta['sector']}")
        if meta.get("country"): m_txt.append(f"{meta['country']}")
        self.lbl_meta.config(text="  |  ".join(m_txt))

    def apply_hot_words(self, event=None):
        self.hot_words = [w.strip().lower() for w in self.entry_hot.get().split(",") if w.strip()]
        self.refresh_ui()

    def refresh_ui(self):
        self.tree.delete(*self.tree.get_children())
        self._displayed_indices = []
        today_date = datetime.now().date()
        yesterday_str = (today_date - timedelta(days=1)).isoformat()
        for idx, item in enumerate(self.current_items):
            show = False
            if self.var_all.get(): show = True
            elif self.var_48.get():
                if item['date'] >= yesterday_str: show = True
            else:
                if item['is_today']: show = True
            if not show: continue
            tag = "today" if item['is_today'] else "old"
            for w in self.hot_words:
                if w in item['headline'].lower(): tag = "hot"; break
            self._displayed_indices.append(idx)
            self.tree.insert("", "end", values=(item['date'], item['age'] or item['time'], item['headline']), tags=(tag,), iid=str(idx))

    def _safe_open_url(self, url):
        try:
            webbrowser.open(url)
        except (OSError, webbrowser.Error):
            pass

    def open_sec_shelf_link(self, event=None):
        if not self.current_symbol or self.current_symbol == "—": return
        query = self.current_cik if self.current_cik else self.current_symbol
        safe_query = url_quote(str(query), safe="")
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={safe_query}&type=S-3&dateb=&owner=exclude&count=10"
        self._safe_open_url(url)

    def open_sec_recent(self, event=None):
        if not self.current_symbol or self.current_symbol == "—": return
        query = self.current_cik if self.current_cik else self.current_symbol
        safe_query = url_quote(str(query), safe="")
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={safe_query}&count=10"
        self._safe_open_url(url)

    def open_finviz_link(self, event=None):
        if not self.current_symbol or self.current_symbol == "—": return
        safe_sym = url_quote(str(self.current_symbol), safe="")
        url = f"https://finviz.com/quote.ashx?t={safe_sym}"
        self._safe_open_url(url)

    def on_double_click(self, event):
        sel = self.tree.focus()
        if not sel: return
        idx = sel if sel.isdigit() else None
        if idx is not None and int(idx) < len(self.current_items):
            item = self.current_items[int(idx)]
            if item.get('url'):
                self._safe_open_url(item['url'])

    def load_settings(self):
        try:
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
                    geo = data.get("geometry")
                    if geo:
                        self.geometry(geo)
                    self.base_font_size = data.get("font_size", self.base_font_size)
                    self.theme_mode = data.get("theme", self.theme_mode)
                    if self.theme_mode in THEMES:
                        self.colors = THEMES[self.theme_mode]
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass

    def on_close(self):
        self.fetcher.close()
        try:
            data = {
                "geometry": self.geometry(),
                "font_size": self.base_font_size,
                "theme": self.theme_mode,
            }
            with open(SETTINGS_FILE, "w") as f: json.dump(data, f)
        except (OSError, TypeError):
            pass
        self.destroy()

if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()