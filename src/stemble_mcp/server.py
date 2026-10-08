"""Stemble MCP (read-only): reads app.stemble.com's Inertia/JSON endpoints using a saved login session.

Run `stemble-mcp login` once (opens a browser window, you sign in yourself; it closes on its own).
After that the MCP runs headless over plain HTTP. Rotated cookies are written back to state.json after
every request so the session keeps sliding, and an expired session is silently re-established from the
saved browser profile when possible.

Every request to Stemble is a GET. Nothing here saves or submits answers.
"""
import hashlib, html, ipaddress, json, os, re, socket, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse
import httpx
from mcp.server.fastmcp import FastMCP

BASE = "https://app.stemble.com"
BASE_HOST = urlparse(BASE).hostname
HOME = Path(os.environ.get("STEMBLE_HOME", Path.home() / ".stemble-mcp")).expanduser()
STATE = HOME / "state.json"
PROFILE = HOME / "profile"
OUTPUT = Path(os.environ.get("STEMBLE_OUTPUT_DIR", Path.home() / "stemble-exports")).expanduser()
LOGIN_CMD = "stemble-mcp login"
DL_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".txt", ".docx", ".xlsx", ".pptx", ".zip", ".csv"}
IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}
MAX_BYTES = 25_000_000
# Inertia shared props that repeat on every page and only burn tokens (top level only).
NOISE = {"auth", "user", "flash", "errors", "ziggy", "jetstream", "notifications", "features",
         "translations", "sidebar", "appConfig", "config", "csrf_token", "impersonating", "toast"}


class Expired(RuntimeError):
    pass


def _log(msg):
    print(msg, file=sys.stderr, flush=True)  # stdout belongs to the MCP protocol


# ---------- paths and permissions ----------

def _lock(path):
    try:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
    except OSError:
        pass


def _own_path(p):
    """Only Stemble-relative paths: one leading slash, no scheme, no host."""
    if not isinstance(p, str) or not p.startswith("/") or p.startswith("//") or "://" in p or "\\" in p:
        raise ValueError("path must be a Stemble-relative path starting with a single '/'")
    return p


def _safe(rel):
    """Resolve rel inside OUTPUT (STEMBLE_OUTPUT_DIR); refuse anything that escapes it."""
    root = OUTPUT.resolve()
    rel = (rel or "").strip()
    if rel.startswith("~"):
        rel = str(Path(rel).expanduser())
    p = Path(rel)
    p = (p if p.is_absolute() else root / p).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path must stay inside {root} (set STEMBLE_OUTPUT_DIR to change it)")
    return p


def _write_state(st):
    """Atomic, owner-only write of the session file."""
    tmp = STATE.with_name(f"state.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


# ---------- browser (login, silent refresh, client-rendered tasks) ----------

def _browser_candidates():
    """STEMBLE_BROWSER_PATH, then system Chrome, system Edge, then Playwright's bundled Chromium."""
    exe = os.environ.get("STEMBLE_BROWSER_PATH")
    if exe:
        return [{"executable_path": exe}]
    return [{"channel": "chrome"}, {"channel": "msedge"}, {}]


def _open(p, headless, persistent):
    last = None
    for opts in _browser_candidates():
        try:
            if persistent:
                return p.chromium.launch_persistent_context(str(PROFILE), headless=headless, **opts)
            return p.chromium.launch(headless=headless, **opts)
        except Exception as e:
            last = e
    raise RuntimeError("No usable browser. Install Chrome, run `uvx playwright install chromium`, "
                       "or set STEMBLE_BROWSER_PATH.") from last


def _signed_in(page):
    return "/login" not in page.url and "/auth" not in page.url and "accounts.google" not in page.url


def _save_state(ctx):
    ctx.storage_state(path=str(STATE))
    _lock(STATE)


def login():
    """Interactive: user signs in; the window closes itself once Stemble shows a signed-in page."""
    from playwright.sync_api import sync_playwright
    HOME.mkdir(parents=True, exist_ok=True)
    _lock(HOME)
    with sync_playwright() as p:
        ctx = _open(p, False, True)
        _lock(PROFILE)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/login")
        print("Sign in; this window closes by itself when you're in.", flush=True)
        deadline = time.time() + 900
        done = False
        while time.time() < deadline and ctx.pages and not done:
            try:
                # sign-in can finish in a popup or new tab, so check every page
                for pg in ctx.pages:
                    if urlparse(pg.url).hostname == BASE_HOST and _signed_in(pg):
                        done = True
                # save as we go so closing the window by hand still keeps the session
                _save_state(ctx)
                time.sleep(1)
            except Exception:
                break
        try:
            time.sleep(2)
            _save_state(ctx)
            ctx.close()
        except Exception:
            pass
    print(f"Saved session to {STATE}")


def _run_with_timeout(fn, seconds):
    """Run blocking Playwright code in its own thread (sync Playwright can't share the MCP's event loop)."""
    ex = ThreadPoolExecutor(1)
    try:
        return ex.submit(fn).result(timeout=seconds)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def silent_refresh():
    """Reopen the saved profile headless; if its cookies still sign us in, save them."""
    if not PROFILE.exists():
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    def run():
        with sync_playwright() as p:
            ctx = _open(p, True, True)
            try:
                ctx.set_default_timeout(20000)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                ok = _signed_in(page)
                if ok:
                    _save_state(ctx)
                return ok
            finally:
                ctx.close()
    try:
        return _run_with_timeout(run, 60)
    except Exception as e:
        _log(f"silent_refresh failed: {e}")
        return False


# ---------- HTTP client ----------

class Client:
    def __init__(self):
        if not STATE.exists():
            raise Expired(f"Not logged in. Run: {LOGIN_CMD}")
        self.mtime = STATE.stat().st_mtime
        jar = httpx.Cookies()
        for ck in json.loads(STATE.read_text(encoding="utf-8"))["cookies"]:
            jar.set(ck["name"], ck["value"], domain=ck["domain"], path=ck["path"])
        self.h = httpx.Client(base_url=BASE, cookies=jar, follow_redirects=False,
                              timeout=httpx.Timeout(30, connect=10),
                              transport=httpx.HTTPTransport(retries=2),
                              limits=httpx.Limits(max_connections=8))
        self.version = None
        self.lock = threading.Lock()

    def save_cookies(self):
        """Persist rotated session/XSRF cookies so the next process (and the next hour) stays signed in."""
        with self.lock:
            try:
                st = json.loads(STATE.read_text(encoding="utf-8"))
                cur = {(k.name, k.domain): k for k in list(self.h.cookies.jar)}
                changed = False
                for ck in st["cookies"]:
                    k = cur.get((ck["name"], ck["domain"]))
                    if k and k.value and (k.value != ck["value"] or (k.expires and k.expires != ck.get("expires"))):
                        ck["value"], changed = k.value, True
                        if k.expires:
                            ck["expires"] = k.expires
                if changed:
                    _write_state(st)
                    self.mtime = STATE.stat().st_mtime
            except Exception as e:
                _log(f"save_cookies: {e}")

    def get(self, path, headers=None):
        """GET a Stemble path. Follows redirects only within Stemble; retries 429/5xx a couple of times."""
        tries = 0
        for _ in range(8):
            r = self.h.get(path, headers=headers)
            if r.status_code in (301, 302, 303, 307, 308):
                u = urlparse(urljoin(BASE + "/", r.headers.get("location", "")))
                if u.hostname != BASE_HOST:
                    raise RuntimeError("Refusing to follow a redirect to another host.")
                if "/login" in u.path:
                    raise Expired("redirected to login")
                path = u.path + (f"?{u.query}" if u.query else "")
                continue
            if r.status_code in (429, 502, 503, 504) and tries < 2:
                tries += 1
                time.sleep(1.5 * tries)
                continue
            return r
        raise RuntimeError("Too many redirects.")

    def _load_version(self, path):
        r = self.get(path)
        if r.status_code in (401, 419):
            raise Expired(f"auth failure {r.status_code}")
        m = re.search(r'<script[^>]*data-page="app"[^>]*>(.*?)</script>', r.text, re.S)
        if m:
            page = json.loads(m.group(1))
        else:
            m = re.search(r'data-page="([^"]+)"', r.text)
            if not m:
                raise RuntimeError("Page is not Inertia; can't parse.")
            page = json.loads(html.unescape(m.group(1)))
        if page.get("component", "").startswith("Login"):
            raise Expired("session expired")
        self.version = page.get("version")
        return page

    def page(self, path):
        """Return the Inertia page (component + props)."""
        if self.version is None:
            p = self._load_version(path)
        else:
            r = self.get(path, headers={"X-Inertia": "true", "X-Inertia-Version": self.version or "",
                                        "X-Requested-With": "XMLHttpRequest",
                                        "Accept": "text/html, application/xhtml+xml"})
            if r.status_code == 409:
                p = self._load_version(path)
            elif r.status_code in (401, 419):
                raise Expired(f"auth failure {r.status_code}")
            else:
                r.raise_for_status()
                try:
                    p = r.json()
                except ValueError:
                    p = self._load_version(path)
                if p.get("component", "").startswith("Login"):
                    raise Expired("login page")
        self.save_cookies()
        return p


mcp = FastMCP("stemble")
_c = None
_c_lock = threading.Lock()
_refresh_lock = threading.Lock()


def c():
    """Shared client; rebuilt automatically if state.json was replaced (e.g. after a re-login)."""
    global _c
    with _c_lock:
        if _c is None or (STATE.exists() and STATE.stat().st_mtime > _c.mtime + 1):
            _c = Client()
        return _c


def fetch(path):
    """Page fetch with one automatic silent re-auth on expiry (one refresh even with parallel workers)."""
    global _c
    path = _own_path(path)
    try:
        cl = c()
    except Expired as e:
        raise RuntimeError(str(e))
    try:
        return cl.page(path)
    except Expired:
        with _refresh_lock:
            if _c is cl:  # nobody else refreshed while we waited
                if not silent_refresh():
                    raise RuntimeError(f"Stemble session expired. Ask the user to run: {LOGIN_CMD}")
                with _c_lock:
                    _c = None
        try:
            return c().page(path)
        except Expired:
            raise RuntimeError(f"Stemble session expired. Ask the user to run: {LOGIN_CMD}")


# ---------- output shaping ----------

def _strip(h):
    h = re.sub(r"<img[^>]*>", " [image] ", h or "")
    h = re.sub(r"<sup[^>]*>(.*?)</sup>", r"^(\1)", h, flags=re.S)
    h = re.sub(r"<sub[^>]*>(.*?)</sub>", r"_\1", h, flags=re.S)
    h = re.sub(r"</(p|h\d|li|tr|blockquote|div)>|<br\s*/?>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _slim(x, top=True):
    """Drop empties and convert HTML strings to text, recursively. NOISE keys go only at the top level."""
    if isinstance(x, dict):
        out = {k: _slim(v, False) for k, v in x.items() if not (top and k in NOISE)}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(x, list):
        return [_slim(v, False) for v in x]
    if isinstance(x, str) and "<" in x and re.search(r"</?[a-z][^>]*>", x):
        return _strip(x)
    return x


def dump(x, limit=40000):
    s = json.dumps(x, separators=(",", ":"), ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "\n...[truncated; narrow the request or use raw=True on get_page]"


def props(path, raw=False):
    p = fetch(path)
    return p if raw else _slim(p.get("props", {}))


# ---------- tools ----------

@mcp.tool()
def list_courses() -> str:
    """Your Stemble courses and their ids, from the dashboard."""
    return dump(props("/dashboard"))


@mcp.tool()
def get_page(path: str, raw: bool = False) -> str:
    """Fetch any Stemble page's data, e.g. '/courses/<id>/materials'. raw=True returns the full Inertia JSON."""
    return dump(props(path, raw), limit=60000 if raw else 40000)


@mcp.tool()
def list_assignments(course_id: int) -> str:
    """List assignments in a course with due dates and status."""
    return dump(props(f"/courses/{course_id}/assignments"))


@mcp.tool()
def get_assignment(course_id: int, assignment_id: int) -> str:
    """Assignment overview (tasks, due date, grade)."""
    return dump(props(f"/courses/{course_id}/assignments/{assignment_id}"))


@mcp.tool()
def get_task(course_id: int, assignment_id: int, task_id: int) -> str:
    """Full task content (question text, inputs, your saved response)."""
    return dump(props(f"/courses/{course_id}/assignments/{assignment_id}/tasks/{task_id}"))


def _fill(text, symbols, values):
    for s, v in zip(symbols, values):
        text = (text or "").replace("{{%s}}" % s, str(v))
    return text


def _render_dynamic_batch(urls):
    """Legacy DynamicQuestion tasks are drawn client-side; render them all in ONE headless browser."""
    if not urls:
        return {}
    from playwright.sync_api import sync_playwright

    def run():
        res = {}
        with sync_playwright() as p:
            b = _open(p, True, False)
            try:
                ctx = b.new_context(storage_state=str(STATE))
                ctx.set_default_timeout(20000)
                for url in urls:
                    pg = ctx.new_page()
                    try:
                        pg.goto(url)
                        pg.wait_for_load_state("networkidle")
                        if "/login" in pg.url:
                            res[url] = "(render failed: session expired; re-run login)"
                            continue
                        pg.wait_for_timeout(1500)
                        txt = "\n".join(f.inner_text("body") for f in pg.frames[1:])
                        menus = []
                        for f in pg.frames[1:]:
                            for n, box in enumerate(f.query_selector_all("[role=button][aria-haspopup=listbox]"), 1):
                                try:
                                    box.click(); pg.wait_for_timeout(400)
                                    opts = [x.inner_text().strip() for x in f.query_selector_all(
                                        ".menuable__content__active [role=option], .v-menu__content--active .v-list-item")]
                                    menus.append(f"  dropdown {n}: " + " | ".join(opts))
                                    pg.keyboard.press("Escape"); pg.wait_for_timeout(150)
                                except Exception as e:
                                    menus.append(f"  dropdown {n}: (failed: {type(e).__name__})")
                        if menus:
                            txt += "\n\nDROPDOWN OPTIONS:\n" + "\n".join(menus)
                        res[url] = txt.strip() or "(no text rendered)"
                    except Exception as e:
                        res[url] = f"(render failed: {type(e).__name__})"
                    finally:
                        pg.close()
            finally:
                b.close()
        return res

    return _run_with_timeout(run, 180)


def _public_https(u):
    """True if u is an https URL whose host resolves only to public addresses."""
    p = urlparse(u)
    if p.scheme != "https" or not p.hostname:
        return False
    try:
        for ai in socket.getaddrinfo(p.hostname, 443):
            ip = ipaddress.ip_address(ai[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
    except Exception:
        return False
    return True


def _save_img(out_dir, tid, src):
    u = urlparse(urljoin(BASE + "/", src))
    key = hashlib.md5(src.encode()).hexdigest()[:8]
    for ext in set(IMG_EXT.values()):  # already fetched
        fn = out_dir / "img" / f"{tid}_{key}{ext}"
        if fn.exists():
            return f"  (image: {fn})"
    try:
        if u.scheme == "https" and u.hostname == BASE_HOST:
            r = c().get(u.path + (f"?{u.query}" if u.query else ""))  # authenticated, same host only
        elif _public_https(u.geturl()):
            r = httpx.get(u.geturl(), follow_redirects=False, timeout=15)  # no cookies sent
        else:
            return "  (image skipped: disallowed host)"
        r.raise_for_status()
        ext = IMG_EXT.get(r.headers.get("content-type", "").split(";")[0].strip().lower())
        if not ext or len(r.content) > 10_000_000:
            return f"  (image skipped: {r.headers.get('content-type')})"
        fn = out_dir / "img" / f"{tid}_{key}{ext}"
        fn.write_bytes(r.content)
        return f"  (image: {fn})"
    except Exception as e:
        return f"  (image failed: {type(e).__name__})"


def _task_block(course_id, assignment_id, n, t, out_dir):
    """Returns (lines, dynamic_url_or_None)."""
    url_path = f"/courses/{course_id}/assignments/{assignment_id}/tasks/{t['id']}"
    pg = fetch(url_path)
    p = pg["props"]
    head = "=" * 60
    if "task" not in p:
        return [head, f"Task {n} (id {t['id']}): not readable ({pg['component']})"], None
    st = p.get("taskState") or {}
    syms = st.get("symbols") or []
    vals = [v.get("sympyValue") for v in st.get("values") or []]
    sub = p["assignmentTask"]["status"]
    out = [head, f"Task {n}: {p['task']['title']}  [id {t['id']}, {t['pointValue']} pt, {sub}, "
                 f"{'unlocked' if t['unlocked'] else 'locked'}]"]
    if p["task"]["taskType"] == "DynamicQuestion":
        return out, BASE + url_path
    if syms:
        out.append("Your variables: " + ", ".join(f"{s}={v}" for s, v in zip(syms, vals)))
    for u in p["task"]["content"].get("uiConfigs", []):
        ty = u["type"]
        if ty == "RichText":
            out.append(_fill(_strip(u.get("html")), syms, vals))
            for src in re.findall(r'<img[^>]+src="([^"]+)"', u.get("html") or ""):
                out.append(_save_img(out_dir, t["id"], html.unescape(src)))
        elif ty == "Image":
            out.append(_save_img(out_dir, t["id"], f"/tasks/{t['id']}/files/{u['fileUploadId']}"))
        elif ty == "AiGrader":
            continue
        else:
            q = u.get("question") or u.get("label") or ""
            out.append(f"  INPUT [{ty}] " + _fill(_strip(q), syms, vals))
            for o in u.get("options") or []:
                out.append(f"     {o.get('key')}) {_strip(o.get('label'))}")
            for k in ("prependText", "appendText", "maxLength", "acceptedFileExtensions"):
                if u.get(k):
                    out.append(f"     {k}: {_fill(str(u[k]), syms, vals)}")
    return out, None


@mcp.tool()
def export_assignment(course_id: int, assignment_id: int, folder: str = "") -> str:
    """Read-only. Dump every task of an assignment (questions, options, your variable values,
    lock status) to <folder>/<id>-questions.txt and save embedded images to <folder>/img/.
    folder: a subfolder of STEMBLE_OUTPUT_DIR (default ~/stemble-exports); it cannot leave that directory.
    Tasks are fetched in parallel; client-rendered tasks share one headless browser.
    This MCP never saves or submits responses; the user answers in Stemble themselves."""
    out_dir = _safe(folder)
    asg = fetch(f"/courses/{course_id}/assignments/{assignment_id}")["props"]["assignment"]
    (out_dir / "img").mkdir(parents=True, exist_ok=True)
    out = [f"{asg['name']}  (assignment {assignment_id})", f"Due: {asg.get('dueDate')}  Points: {asg.get('totalPoints')}"]
    if asg.get("hasUnmetPreconditions") or asg.get("status") == "blocked":
        out.append("STATUS: blocked (prerequisite not met); tasks may not be readable yet.")

    def safe(nt):
        try:
            return _task_block(course_id, assignment_id, nt[0], nt[1], out_dir)
        except Exception as e:  # one bad task must not sink the export
            return ["=" * 60, f"Task {nt[0]} (id {nt[1].get('id')}): FAILED ({type(e).__name__}: {e})"], None

    with ThreadPoolExecutor(6) as ex:
        blocks = list(ex.map(safe, enumerate(asg["tasks"], 1)))
    try:
        rendered = _render_dynamic_batch([u for _, u in blocks if u])
    except Exception as e:
        rendered = {u: f"(render failed: {type(e).__name__}: {e})" for _, u in blocks if u}
    for lines, url in blocks:
        out.append("")
        out.extend(lines)
        if url:
            out.append(rendered.get(url, "(render failed)"))
    text = "\n".join(out)
    path = out_dir / f"{assignment_id}-questions.txt"
    path.write_text(text, encoding="utf-8")
    preview = text if len(text) <= 15000 else text[:15000] + "\n...[preview truncated; read the file]"
    return f"Saved to {path} ({len(text)} chars)\n\n" + preview


@mcp.tool()
def download_file(path: str, dest: str) -> str:
    """Download an authenticated Stemble file (e.g. lab manual PDF, task file). path is a Stemble path
    like '/tasks/1/files/2'; dest is a file path inside STEMBLE_OUTPUT_DIR (default ~/stemble-exports)."""
    dest_p = _safe(dest)
    if dest_p.suffix.lower() not in DL_EXT:
        raise ValueError(f"dest extension must be one of {sorted(DL_EXT)}")
    cl = c()
    r = cl.get(_own_path(path))
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise ValueError("file too large")
    cl.save_cookies()
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    dest_p.write_bytes(r.content)
    return f"saved {len(r.content)} bytes to {dest_p} ({r.headers.get('content-type')})"


def main():
    a = sys.argv[1:]
    if a[:1] == ["login"]:
        login()
    elif a[:1] == ["export"] and len(a) >= 3:
        print(export_assignment(int(a[1]), int(a[2]), *a[3:4]))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
