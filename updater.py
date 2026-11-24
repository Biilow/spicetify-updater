#!/usr/bin/env python3
"""Spicetify Updater: puts Spicetify back after Spotify updates itself.

The LaunchAgent starts "Spicetify Updater.app", which runs this script. That app
exists because macOS only lets apps with the App Management permission write
inside Spotify.app; a LaunchAgent running python directly gets "Operation not
permitted". Everything this script starts inherits the app's grant.

Commands:
  run         what the LaunchAgent calls: fixes things only if they need fixing
  apply-now   re-apply right away (quits and reopens Spotify if it's open)
  status      print the current state as JSON
  pause       stop re-applying automatically (apply-now still works)
  resume      undo pause

Stdlib only, and written for the Python 3.9 that ships at /usr/bin/python3.
"""

import base64
import fcntl
import hashlib
import io
import json
import os
import platform
import plistlib
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.request

HOME = os.path.expanduser("~")
SPOTIFY_APP = "/Applications/Spotify.app"
INFO_PLIST = SPOTIFY_APP + "/Contents/Info.plist"
APPS_DIR = SPOTIFY_APP + "/Contents/Resources/Apps"
SPICETIFY_DIR = HOME + "/.spicetify"
SPICETIFY = SPICETIFY_DIR + "/spicetify"
SUPPORT = HOME + "/Library/Application Support/Spicetify Updater"
STATE_FILE = SUPPORT + "/state.json"
LOCK_FILE = SUPPORT + "/lock"
PAUSE_FILE = SUPPORT + "/paused"
LOG_FILE = HOME + "/Library/Logs/Spicetify Updater.log"
RELEASES_URL = "https://api.github.com/repos/spicetify/cli/releases/latest"
ARCH = "arm64" if platform.machine() == "arm64" else "amd64"

SETTLE_SECONDS = 15    # give Spotify's own installer time to finish swapping the bundle
BUSY_UPTIME = 180      # Spotify open longer than this means you may be listening: don't yank it
READY_TIMEOUT = 90     # how long Spotify gets to show a working, spiced UI (the first launch
                       # after patching can be slow; a false failure means a needless rollback)


def log(msg):
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def trim_log():
    """Keep the log under ~2 MB. launchd appends, so rewriting it in place is safe."""
    try:
        if os.path.getsize(LOG_FILE) > 2_000_000:
            with open(LOG_FILE, "rb") as f:
                f.seek(-500_000, os.SEEK_END)
                tail = f.read()
            with open(LOG_FILE, "wb") as f:
                f.write(tail[tail.find(b"\n") + 1:])
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    os.makedirs(SUPPORT, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def record(state, result, spotify, spicetify=None, detail=None):
    state["last"] = {"result": result, "spotify": spotify, "spicetify": spicetify,
                     "detail": detail, "at": now()}
    save_state(state)


# ---------------------------------------------------------------- Spotify

def spotify_version():
    with open(INFO_PLIST, "rb") as f:
        return plistlib.load(f)["CFBundleShortVersionString"]


def apps_status():
    """Same rule Spicetify uses: .spa files only = stock, folders only = applied."""
    try:
        entries = os.listdir(APPS_DIR)
    except FileNotFoundError:
        return "invalid"
    spa = sum(1 for e in entries if e.endswith(".spa") and os.path.isfile(os.path.join(APPS_DIR, e)))
    dirs = sum(1 for e in entries if os.path.isdir(os.path.join(APPS_DIR, e)))
    if spa and dirs:
        return "mixed"
    if spa:
        return "stock"
    if dirs:
        return "applied"
    return "invalid"


def can_write_spotify():
    """Same probe Spicetify runs. Fails when App Management hasn't been granted."""
    try:
        os.rmdir(tempfile.mkdtemp(prefix=".spicetify-updater-", dir=os.path.dirname(APPS_DIR)))
        return True
    except PermissionError:
        return False


def spotify_pid():
    r = subprocess.run(["/usr/bin/pgrep", "-x", "-U", str(os.getuid()), "Spotify"],
                       capture_output=True, text=True)
    pids = [int(p) for p in r.stdout.split()]
    return pids[0] if pids else None


def uptime(pid):
    """Seconds since the process started, from ps's [[dd-]hh:]mm:ss."""
    s = subprocess.run(["/bin/ps", "-o", "etime=", "-p", str(pid)],
                       capture_output=True, text=True).stdout.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def wait_exit(pid, timeout=None):
    """Block until the process exits. A kernel event (kqueue), not a polling loop."""
    kq = select.kqueue()
    try:
        ev = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                           flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                           fflags=select.KQ_NOTE_EXIT)
        try:
            return bool(kq.control([ev], 1, timeout))
        except ProcessLookupError:
            return True  # already gone
    finally:
        kq.close()


def quit_spotify():
    pid = spotify_pid()
    if not pid:
        return
    log("Quitting Spotify (pid %d)" % pid)
    os.kill(pid, signal.SIGTERM)
    if not wait_exit(pid, 20):
        log("Spotify ignored the quit; forcing it")
        os.kill(pid, signal.SIGKILL)
        wait_exit(pid, 10)
    time.sleep(1)  # its helper processes exit right after the main one


def launch_spotify(args=(), hidden=False):
    cmd = ["/usr/bin/open", "-g"] + (["-j"] if hidden else []) + ["-a", SPOTIFY_APP]
    if args:
        cmd += ["--args"] + list(args)
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------- Spicetify CLI

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROGRESS = re.compile(r"^(Patching files \[|[-\\|/] )")  # progress bars and spinner frames


def spicetify(*args):
    log("$ spicetify " + " ".join(args))
    # stdin is closed so any prompt takes its default instead of hanging
    r = subprocess.run([SPICETIFY] + list(args), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=600)
    out = ANSI.sub("", (r.stdout or "") + (r.stderr or "")).replace("\r", "\n")
    # the spinner rewrites its line, so a finished step arrives glued after its frames
    out = re.sub(r"\s{3,}(success|warning|error|info|note)\b", r"\n\1", out)
    for line in out.splitlines():
        line = line.strip()
        if line and not PROGRESS.match(line):
            log("  " + line)
    return r.returncode, out


def cli_version():
    return subprocess.run([SPICETIFY, "-v"], capture_output=True, text=True).stdout.strip()


def vt(version):
    return tuple(int(x) for x in version.split(".")[:3])


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "spicetify-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release():
    """Latest Spicetify release, its Mac download, and the Spotify range it was tested on."""
    rel = json.loads(fetch(RELEASES_URL))
    version = rel["tag_name"].lstrip("v")
    tested = re.search(r"Spotify for \*\*macOS\*\*:\s*`([\d.]+)`\s*->\s*`([\d.]+)`", rel.get("body") or "")
    asset_name = "spicetify-%s-darwin-%s.tar.gz" % (version, ARCH)
    asset = next((a for a in rel.get("assets", []) if a["name"] == asset_name), None)
    return {"version": version, "asset": asset,
            "min": tested.group(1) if tested else None,
            "max": tested.group(2) if tested else None}


def is_tested(rel, spotify):
    """True/False when the release notes list a range, None when unknown."""
    if not rel or not rel["min"]:
        return None
    return vt(rel["min"]) <= vt(spotify) <= vt(rel["max"])


def upgrade_cli(rel):
    """Install a newer Spicetify from its GitHub release, checksum-verified.

    Not `spicetify upgrade`: when that installs a new version it also re-applies
    and restarts Spotify on its own, which would skip the health check below.
    """
    current = cli_version()
    if vt(rel["version"]) <= vt(current):
        log("Spicetify %s is the latest" % current)
        return
    asset = rel["asset"]
    digest = (asset or {}).get("digest") or ""
    if not digest.startswith("sha256:"):
        log("Spicetify %s has no checksummed Mac download; staying on %s" % (rel["version"], current))
        return
    data = fetch(asset["browser_download_url"], timeout=120)
    if hashlib.sha256(data).hexdigest() != digest[len("sha256:"):]:
        log("Checksum mismatch on the Spicetify %s download; staying on %s" % (rel["version"], current))
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/") or not (m.isfile() or m.isdir()):
                raise ValueError("unsafe path in Spicetify download: %s" % m.name)
        tar.extractall(SPICETIFY_DIR)
    log("Upgraded Spicetify %s -> %s" % (current, cli_version()))


# ---------------------------------------------------------------- health check
#
# Spotify is started once with a debug port, and the page itself is asked
# whether Spicetify loaded and the UI rendered. That catches the black-window
# failure, which `spicetify apply` happily reports as a success. Spotify is
# quit again afterwards so the debug port never stays open.

READY_JS = r"""
new Promise((resolve) => {
  const started = Date.now();
  const probe = () => {
    const S = window.Spicetify;
    // <main> and data-testid hooks, not class names: Spotify renames classes between versions
    const main = document.querySelector("main");
    return {
      wrapper: !!S,
      platform: !!(S && S.Platform && S.Platform.version),
      player: !!(S && S.Player && typeof S.Player.togglePlay === "function"),
      ui: !!main && main.getElementsByTagName("*").length > 40 &&
          !!document.querySelector('[data-testid="now-playing-bar"]'),
    };
  };
  const tick = () => {
    const s = probe();
    const ok = s.wrapper && s.platform && s.player && s.ui;
    if (ok || Date.now() - started > TIMEOUT_MS) {
      resolve(Object.assign({ ok, ms: Date.now() - started,
        spotify: (window.Spicetify && Spicetify.Platform && Spicetify.Platform.version) || null }, s));
    } else {
      setTimeout(tick, 500);  // bounded: ends at TIMEOUT_MS
    }
  };
  tick();
})
"""


class WebSocket:
    """Just enough of RFC 6455 to talk to Chrome DevTools on localhost."""

    def __init__(self, url, timeout):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)", url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed the connection during the handshake")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise ConnectionError("DevTools refused the connection: %s" % head.split(b"\r\n", 1)[0].decode())

    def send(self, text, opcode=0x1):
        data = text.encode() if isinstance(text, str) else text
        n = len(data)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        message = b""
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("DevTools closed the connection")
            if opcode == 0x9:
                self.send(payload, opcode=0xA)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if b0 & 0x80:
                    return message.decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class PageNotReady(Exception):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_xpui_page(port, deadline):
    """Wait for Spotify to open its debug port and main page (startup, so bounded).
    Returns (websocket url or None, what was last seen)."""
    seen = "nothing yet"
    while time.time() < deadline:
        try:
            targets = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % port, timeout=2).read())
            for t in targets:
                if t.get("type") == "page" and "xpui" in t.get("url", "") and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"], "page found"
            seen = "targets: " + ", ".join("%s %s" % (t.get("type"), t.get("url", "")[:50]) for t in targets)
        except (OSError, ValueError) as e:
            seen = "%s: %s" % (type(e).__name__, e)  # usually: port not open yet
        time.sleep(0.5)
    return None, seen


def evaluate(ws_url, expression, deadline):
    ws = WebSocket(ws_url, timeout=max(1.0, deadline - time.time()) + 5)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression, "awaitPromise": True,
                                       "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            if "error" in msg:  # e.g. the page reloaded mid-check
                raise PageNotReady(msg["error"].get("message", "DevTools error"))
            result = msg["result"]
            if "exceptionDetails" in result:
                raise PageNotReady(result["exceptionDetails"].get("text", "script error"))
            return result["result"]["value"]
    finally:
        ws.close()


def health_check():
    port = free_port()
    launch_spotify(["--remote-debugging-port=%d" % port], hidden=True)
    deadline = time.time() + READY_TIMEOUT
    seen = "nothing yet"
    try:
        while time.time() < deadline:
            ws_url, seen = find_xpui_page(port, deadline)
            if not ws_url:
                break
            remaining_ms = int(max(1.0, deadline - time.time()) * 1000)
            try:
                result = evaluate(ws_url, READY_JS.replace("TIMEOUT_MS", str(remaining_ms)), deadline)
            except (PageNotReady, ConnectionError, socket.timeout) as e:
                log("  page not ready yet (%s); checking again" % e)
                time.sleep(1)
                continue
            return bool(result.get("ok")), result
        pid = spotify_pid()
        args = subprocess.run(["/bin/ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                              text=True).stdout.strip() if pid else "not running"
        log("  no page to check. Last seen: %s. Spotify: %s" % (seen, args))
        return False, {"error": "Spotify never opened its page for checking", "seen": seen, "process": args}
    finally:
        quit_spotify()


def describe(detail):
    if detail.get("error"):
        return detail["error"]
    problems = [text for key, text in (("wrapper", "Spicetify didn't load"),
                                       ("platform", "Spicetify couldn't hook into Spotify"),
                                       ("player", "the player controls were missing"),
                                       ("ui", "Spotify's window stayed blank"))
                if not detail.get(key)]
    return "; ".join(problems) or "the check failed"


def last_error(out):
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    errors = [l for l in lines if "error" in l.lower()]
    return (errors or lines or ["no output"])[-1]


# ---------------------------------------------------------------- the flow

def rollback(state, spotify, cli, tested, relaunch, reason):
    log("Rolling back: " + reason)
    quit_spotify()
    if apps_status() != "stock":
        spicetify("restore", "-n")
    if apps_status() != "stock":
        record(state, "broken", spotify, cli, reason)
        notify("Spotify needs reinstalling",
               "Spicetify couldn't be undone cleanly. Download Spotify again from spotify.com/download. "
               "Details are in ~/Library/Logs/Spicetify Updater.log")
        return 1
    if relaunch:
        launch_spotify()
    state["waiting"] = {"spotify": spotify, "spicetify": cli, "reason": reason, "at": now()}
    record(state, "rolled-back", spotify, cli, reason)
    if tested is False:
        title = "Spicetify doesn't support Spotify %s yet" % spotify
    else:
        title = "Spicetify didn't work on Spotify %s" % spotify
    notify(title, "Spotify is back to normal without it (%s). It'll try again when a new Spicetify comes out." % reason)
    return 1


def apply_flow(state, manual):
    spotify = spotify_version()
    pid = spotify_pid()
    if pid and not manual and uptime(pid) > BUSY_UPTIME:
        notify("Spotify updated to %s" % spotify, "Spicetify will be re-applied when you next quit Spotify.")
        log("Spotify has been open a while, so you may be listening. Waiting for you to quit it.")
        wait_exit(pid)
        time.sleep(5)  # Spotify's own updater sometimes relaunches it straight away
        return decide(load_state(), manual=False, settle=False)
    relaunch = bool(pid)
    if pid:
        quit_spotify()

    cli, tested = cli_version(), None
    try:
        try:
            rel = latest_release()
        except OSError as e:
            rel = None
            log("Couldn't reach GitHub (%s); using the installed Spicetify" % e)
        if rel:
            upgrade_cli(rel)
        cli, tested = cli_version(), is_tested(rel, spotify)
        log("Spotify %s is %s" % (spotify, {True: "on Spicetify's tested list",
                                            False: "newer than Spicetify's tested list (%s to %s)"
                                                   % (rel and rel["min"], rel and rel["max"]),
                                            None: "of unknown support"}[tested]))

        steps = ["backup", "apply"] if apps_status() == "stock" else ["restore", "backup", "apply"]
        code, out = spicetify(*(steps + ["-n"]))
        if code != 0:
            return rollback(state, spotify, cli, tested, relaunch, last_error(out))

        ok, detail = health_check()
        log("Health check: " + json.dumps(detail))
        if not ok:
            return rollback(state, spotify, cli, tested, relaunch, describe(detail))
    except Exception as e:
        log(traceback.format_exc())
        return rollback(state, spotify, cli, tested, relaunch, "unexpected error: %s" % e)

    if relaunch:
        launch_spotify()
    state["good"] = {"spotify": spotify, "spicetify": cli, "at": now()}
    state.pop("waiting", None)
    record(state, "applied", spotify, cli)
    note = " Spotify is newer than Spicetify's tested list, but it loaded fine." if tested is False else ""
    notify("Spicetify is back", "Spotify %s with Spicetify %s.%s" % (spotify, cli, note))
    return 0


def decide(state, manual, settle=True):
    if not os.path.exists(INFO_PLIST):
        log("Spotify isn't installed")
        return 0
    if settle and time.time() - os.path.getmtime(INFO_PLIST) < 120:
        log("Spotify was just updated; giving its installer a moment")
        time.sleep(SETTLE_SECONDS)

    spotify, apps = spotify_version(), apps_status()
    log("Spotify %s, app files %s, Spicetify %s%s" % (spotify, apps, cli_version(),
                                                     " (apply-now)" if manual else ""))
    if not manual and os.path.exists(PAUSE_FILE):
        log("Paused, so not touching anything")
        return 0

    # Checked on every run (login, daily, update) so a missing grant shows up
    # before the next Spotify update needs it, not after.
    writable = can_write_spotify()
    if writable != state.get("can_write"):
        state["can_write"] = writable
        save_state(state)
        log("App Management permission: " + ("granted" if writable else "missing"))
        if not writable and not manual:
            notify("Spicetify Updater needs permission",
                   "Turn it on in System Settings > Privacy & Security > App Management.")
    if not writable:
        if manual:
            notify("Spicetify Updater needs permission",
                   "Turn it on in System Settings > Privacy & Security > App Management. Spotify wasn't touched.")
        record(state, "needs-permission", spotify)
        return 1

    if manual:
        return apply_flow(state, manual=True)

    if apps == "applied":
        if state.get("good", {}).get("spotify") != spotify:  # e.g. applied by hand
            state["good"] = {"spotify": spotify, "spicetify": cli_version(), "at": now()}
            state.pop("waiting", None)
            save_state(state)
        log("Spicetify is already applied")
        return 0
    if apps == "invalid":
        record(state, "broken", spotify)
        notify("Spotify looks broken", "Its app files are missing. Download Spotify again from spotify.com/download.")
        return 1
    if apps == "stock" and state.get("good", {}).get("spotify") == spotify:
        log("Spotify %s had Spicetify before and is stock now, so it was probably restored on purpose. "
            "Run apply-now to put it back." % spotify)
        return 0

    waiting = state.get("waiting")
    if waiting and waiting.get("spotify") == spotify:
        try:
            rel = latest_release()
        except OSError as e:
            log("Couldn't check for a new Spicetify (%s)" % e)
            return 0
        if rel["version"] == waiting.get("spicetify"):
            log("Still waiting for a Spicetify newer than %s for Spotify %s" % (rel["version"], spotify))
            return 0
        log("Spicetify %s is out; trying again" % rel["version"])
    return apply_flow(state, manual=False)


def notify(title, body):
    """Posts through osascript (shown under Script Editor's icon). Native notifications
    would need a paid Apple developer certificate: Notification Center refuses
    ad-hoc-signed apps. Title and body go in as arguments, never into the script text."""
    log("Notify: %s / %s" % (title, body))
    r = subprocess.run(["/usr/bin/osascript",
                        "-e", "on run argv",
                        "-e", 'display notification (item 2 of argv) with title "Spicetify Updater" subtitle (item 1 of argv)',
                        "-e", "end run", title, body],
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        log("  notification failed: " + r.stderr.strip())


def status():
    state = load_state()
    installed = os.path.exists(INFO_PLIST)
    print(json.dumps(dict({
        "spotify": spotify_version() if installed else None,
        "app_files": apps_status() if installed else None,
        "spicetify": cli_version(),
        "paused": os.path.exists(PAUSE_FILE),
    }, **state), indent=2))
    return 0


def main(argv):
    command = argv[1] if len(argv) > 1 else "run"
    if command == "status":
        return status()
    os.makedirs(SUPPORT, exist_ok=True)
    if command == "pause":
        open(PAUSE_FILE, "w").close()
        print("Paused. Spicetify won't be re-applied automatically until you run: resume")
        return 0
    if command == "resume":
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        print("Resumed.")
        return 0
    if command not in ("run", "apply-now"):
        sys.stderr.write(__doc__)
        return 2

    trim_log()
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another run is already going")
        return 0
    try:
        return decide(load_state(), manual=(command == "apply-now"))
    except Exception:
        log(traceback.format_exc())
        notify("Spicetify Updater hit an error", "Details are in ~/Library/Logs/Spicetify Updater.log")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
