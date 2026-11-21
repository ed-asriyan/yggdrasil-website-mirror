#!/usr/bin/env python3
"""End-to-end test of docker-compose.yml.

Brings up docker-compose.yml together with tests/compose.ci.yml (a mock origin,
tests/origin/server.py, serving site.test, api.test and cdn.test) and checks
the result.

Scenario `mirror`: the mirror peers with a Yggdrasil node started on the test
host, and every request goes over Yggdrasil to http://[<mirror address>]:<port>,
as a real visitor's would. Checks, in parallel:

  * routing: each port reaches its domain, with the right Host and SNI;
  * link rewriting: no real address is left in response bodies;
  * redirects to any mirrored domain stay on the mirror;
  * caching: HIT on repeat, keyed by cookie and domain, Set-Cookie responses
    not cached;
  * response headers larger than nginx's default buffers;
  * the page in headless Chromium: every resource loads through the mirror;
  * YGG_NODE_NAME is published in NodeInfo.

Scenario `config`: configuration handling, no Yggdrasil connectivity needed:
missing required variables, domains without a scheme, and a minimal setup
(TARGET_DOMAIN only) with the generated Yggdrasil and nginx configs.

Requirements on the host: docker compose, yggdrasil, openssl; for `mirror`
also Python Playwright with Chromium, root (or passwordless sudo) and IPv6.

Usage:
  tests/e2e.py --scenario mirror
  tests/e2e.py --scenario config --artifacts out/
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
COMPOSE_FILES = [REPO / "docker-compose.yml", TESTS / "compose.ci.yml"]
YGG_LISTEN_PORT = 12345

TARGET_DOMAIN = "https://site.test"
ADDITIONAL_DOMAINS = "https://api.test:8443 http://cdn.test:8080"
# Characters the compose entrypoint has to escape for JSON and for sed.
NODE_NAME = 'e2e "mirror" \\ a&b|c ёлка'


@dataclass(frozen=True)
class Upstream:
    mirror_port: int  # port the mirror listens on
    scheme: str
    host: str
    port: int


# TARGET_DOMAIN on 80, ADDITIONAL_DOMAINS on 81, 82
UPSTREAMS = [
    Upstream(80, "https", "site.test", 443),
    Upstream(81, "https", "api.test", 8443),
    Upstream(82, "http", "cdn.test", 8080),
]
LEAK_RE = re.compile(r"(site|api|cdn)\.test")


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("check", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def as_root(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


class CheckFailed(Exception):
    pass


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFailed(msg)


def describe(e: Exception) -> str:
    if isinstance(e, CheckFailed):
        return str(e)
    if isinstance(e, subprocess.CalledProcessError):
        cmd = " ".join(e.cmd[:2] + [a for a in e.cmd[2:] if not a.startswith("/")])
        out = (e.stderr or e.stdout or "").strip().splitlines()
        return f"`{cmd}` exited with {e.returncode}: {out[-1] if out else ''}"
    return f"{type(e).__name__}: {e}"


def retry(name: str, fn, deadline: float, interval: float = 3) -> Result:
    """Run `fn` until it returns (detail) without raising, or the deadline passes."""
    attempt = 0
    while True:
        attempt += 1
        try:
            detail = fn()
            return Result(name, True, f"{detail} (attempt {attempt})" if attempt > 1 else detail)
        except Exception as e:  # noqa: BLE001 - report any failure
            last = describe(e)
        if time.monotonic() > deadline:
            return Result(name, False, last)
        time.sleep(interval)


def once(name: str, fn) -> Result:
    try:
        return Result(name, True, fn())
    except Exception as e:  # noqa: BLE001 - report any failure
        return Result(name, False, describe(e))


# --- yggdrasil ---------------------------------------------------------------


def ygg_genconf() -> dict:
    return json.loads(run(["yggdrasil", "-genconf", "-json"], capture_output=True).stdout)


def ygg_identity(private_key: str, workdir: Path) -> tuple[str, str]:
    """(IPv6 address, public key) of a node with this private key."""
    path = workdir / f"ygg-{private_key[:8]}.json"
    path.write_text(json.dumps({"PrivateKey": private_key}))
    address = run(["yggdrasil", "-useconffile", str(path), "-address"], capture_output=True).stdout.strip()
    public = run(["yggdrasil", "-useconffile", str(path), "-publickey"], capture_output=True).stdout.strip()
    return address, public


class YggNode:
    """The test host's own Yggdrasil node: the mirror peers with it."""

    def __init__(self, workdir: Path, artifacts: Path):
        self.workdir = workdir
        self.artifacts = artifacts
        self.socket = workdir / "yggdrasil-client.sock"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        conf = ygg_genconf()
        conf.update(
            Listen=[f"tcp://0.0.0.0:{YGG_LISTEN_PORT}"],
            Peers=[],
            MulticastInterfaces=[],
            AdminListen=f"unix://{self.socket}",
            IfName="ygg-e2e",
        )
        path = self.workdir / "yggdrasil-client.json"
        path.write_text(json.dumps(conf))
        log_file = open(self.artifacts / "yggdrasil-client.log", "w")
        self.proc = subprocess.Popen(
            as_root(["yggdrasil", "-useconffile", str(path)]), stdout=log_file, stderr=subprocess.STDOUT
        )
        address, _ = ygg_identity(conf["PrivateKey"], self.workdir)
        log(f"Yggdrasil test node: {address}, listening on :{YGG_LISTEN_PORT}")

    def ctl(self, *args: str) -> dict:
        out = run(
            as_root(["yggdrasilctl", "-json", f"-endpoint=unix://{self.socket}", *args]),
            capture_output=True, timeout=30,
        ).stdout
        return json.loads(out)

    def stop(self) -> None:
        if self.proc:
            run(as_root(["kill", str(self.proc.pid)]), check=False)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


# --- deployment --------------------------------------------------------------


class Deployment:
    def __init__(self, project: str, workdir: Path, artifacts: Path, env: dict[str, str]):
        self.project = project
        self.workdir = workdir
        self.artifacts = artifacts
        self.env = {
            **{k: v for k, v in os.environ.items()
               if k not in ("YGG_PRIV_KEY", "YGG_PEERS", "YGG_NODE_NAME", "TARGET_DOMAIN", "ADDITIONAL_DOMAINS")},
            "E2E_ORIGIN_DIR": str(TESTS / "origin"),
            "E2E_ORIGIN_CERTS": str(origin_certs(workdir)),
            **env,
        }

    def compose(self, *args: str, **kw) -> subprocess.CompletedProcess:
        files = [arg for f in COMPOSE_FILES for arg in ("-f", str(f))]
        return run(["docker", "compose", "-p", self.project, *files, *args], env=self.env, **kw)

    def up(self) -> None:
        log(f"Starting {self.project}")
        self.compose("up", "-d", "--quiet-pull", capture_output=True)

    def logs(self, service: str) -> str:
        return self.compose("logs", "--no-log-prefix", "--no-color", service, capture_output=True).stdout

    def container(self, service: str) -> dict:
        cid = self.compose("ps", "-a", "-q", service, capture_output=True).stdout.strip()
        expect(bool(cid), f"no {service} container")
        return json.loads(run(["docker", "inspect", cid], capture_output=True).stdout)[0]

    def exec(self, service: str, *cmd: str) -> str:
        return self.compose("exec", "-T", service, *cmd, capture_output=True).stdout

    def dump_logs(self) -> None:
        out = self.compose("logs", "--no-color", "--timestamps", check=False, capture_output=True)
        (self.artifacts / f"{self.project}.log").write_text(out.stdout + out.stderr)
        ps = self.compose("ps", "-a", check=False, capture_output=True)
        (self.artifacts / f"{self.project}-ps.txt").write_text(ps.stdout + ps.stderr)

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False, capture_output=True)


def origin_certs(workdir: Path) -> Path:
    """Self-signed certificate of the mock origin (nginx does not verify upstreams)."""
    certs = workdir / "certs"
    if not (certs / "origin.crt").exists():
        certs.mkdir(parents=True, exist_ok=True)
        run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=site.test",
            "-addext", "subjectAltName=DNS:site.test,DNS:api.test,DNS:cdn.test",
            "-keyout", str(certs / "origin.key"), "-out", str(certs / "origin.crt"),
        ], capture_output=True)
        # read by the origin container, whatever its user
        (certs / "origin.key").chmod(0o644)
    return certs


# --- scenario: mirror --------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: http.client.HTTPMessage
    body: str


class Mirror:
    """HTTP client for the mirror, over Yggdrasil."""

    def __init__(self, address: str):
        self.address = address
        # what the mirror substitutes for the real domains ($host)
        self.host = f"[{address}]"

    def url(self, port: int, path: str = "/") -> str:
        return f"http://{self.host}{'' if port == 80 else f':{port}'}{path}"

    def get(self, port: int, path: str, headers: dict[str, str] | None = None) -> Response:
        # http.client: no redirects followed, no proxy from the environment
        conn = http.client.HTTPConnection(self.address, port, timeout=15)
        try:
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            return Response(r.status, r.headers, r.read().decode("utf-8", "replace"))
        finally:
            conn.close()

    def rewritten(self, url: str) -> str:
        """What the mirror must turn a real URL (or bare host[:port]) into."""
        for u in UPSTREAMS:
            port = "" if u.mirror_port == 80 else f":{u.mirror_port}"
            default = 443 if u.scheme == "https" else 80
            real = u.host if u.port == default else f"{u.host}:{u.port}"
            for scheme in ("https", "http"):
                url = url.replace(f"{scheme}://{real}", f"http://{self.host}{port}")
            url = url.replace(f'"{real}"', f'"{self.host}:{u.mirror_port}"')
        return url


def check_upstream(m: Mirror, u: Upstream) -> str:
    r = m.get(u.mirror_port, "/links")
    expect(r.status == 200, f"HTTP {r.status}")
    h = r.headers
    expect(h["X-Echo-Port"] == str(u.port), f"reached origin port {h['X-Echo-Port']}, expected {u.port}")
    expect(h["X-Echo-Scheme"] == u.scheme, f"upstream scheme {h['X-Echo-Scheme']}, expected {u.scheme}")
    expect(h["X-Echo-Host"].split(":")[0] == u.host, f"upstream Host {h['X-Echo-Host']!r}, expected {u.host!r}")
    sni = h["X-Echo-Sni"]
    expect(sni == (u.host if u.scheme == "https" else ""), f"upstream SNI {sni!r}")
    expect(h["X-Echo-Accept-Encoding"] == "", f"Accept-Encoding sent upstream: {h['X-Echo-Accept-Encoding']!r}")
    return f"{u.scheme}://{u.host}:{u.port}, Host {h['X-Echo-Host']!r}, SNI {sni!r}"


def check_rewrite(m: Mirror, u: Upstream) -> str:
    r = m.get(u.mirror_port, "/links")
    expect(r.status == 200, f"HTTP {r.status}")
    original = (TESTS / "origin" / "server.py").read_text()
    links = re.search(r'^LINKS = """\\\n(.*?)"""', original, re.S | re.M)[1]
    want = m.rewritten(links)
    if r.body != want:
        diff = [f"{o!r} -> got {g!r}, want {w!r}"
                for o, g, w in zip(links.splitlines(), r.body.splitlines(), want.splitlines()) if g != w]
        raise CheckFailed("; ".join(diff) or f"got {r.body!r}, want {want!r}")
    return f"{len(want.splitlines())} links rewritten"


def check_no_leaks(m: Mirror, u: Upstream) -> str:
    paths = ["/", "/app.js", "/links"]
    for path in paths:
        r = m.get(u.mirror_port, path)
        expect(r.status == 200, f"{path}: HTTP {r.status}")
        leaks = sorted({mt.group(0) for mt in LEAK_RE.finditer(r.body)})
        expect(not leaks, f"{path}: real address(es) left in the body: {leaks}")
    return f"{', '.join(paths)} clean"


def check_redirect(m: Mirror, u: Upstream, to: Upstream | None = None) -> str:
    """A redirect to a mirrored domain (`to`, or the same one) must stay on the mirror."""
    to = to or u
    query = f"to={to.host.split('.')[0]}&" if to != u else ""
    r = m.get(u.mirror_port, f"/redirect?{query}{uuid.uuid4().hex}")
    expect(r.status == 302, f"HTTP {r.status}")
    want = m.url(to.mirror_port, "/target")
    expect(r.headers["Location"] == want, f"Location {r.headers['Location']!r}, expected {want!r}")
    return f"Location {want!r}"


def check_cache(m: Mirror) -> str:
    path = f"/cache?{uuid.uuid4().hex}"

    def get(cookie: str | None = None) -> tuple[str, str]:
        r = m.get(80, path, {"Cookie": cookie} if cookie else None)
        expect(r.status == 200, f"HTTP {r.status}")
        return r.headers["X-Cache-Status"], r.headers["X-Origin-Count"]

    steps = [
        (None, ("MISS", "1"), "first request"),
        (None, ("HIT", "1"), "repeat"),
        ("session=a", ("MISS", "2"), "other cookie"),
        ("session=a", ("HIT", "2"), "repeat with that cookie"),
        (None, ("HIT", "1"), "back to no cookie"),
    ]
    for cookie, want, what in steps:
        got = get(cookie)
        expect(got == want, f"{what}: X-Cache-Status/X-Origin-Count {got}, expected {want}")
    return "MISS, HIT; keyed by cookie"


def check_cache_per_domain(m: Mirror) -> str:
    """The same path on different domains (ports of the same mirror host) must not share cache entries."""
    path = f"/cache?{uuid.uuid4().hex}"
    for u in UPSTREAMS:
        r = m.get(u.mirror_port, path)
        expect(r.status == 200, f":{u.mirror_port}: HTTP {r.status}")
        got = (r.headers["X-Echo-Port"], r.headers["X-Cache-Status"])
        want = (str(u.port), "MISS")
        expect(got == want, f":{u.mirror_port}: origin port/X-Cache-Status {got}, expected {want}")
    return "every domain has its own entry"


def check_set_cookie_not_cached(m: Mirror) -> str:
    path = f"/set-cookie?{uuid.uuid4().hex}"
    for n in ("1", "2"):
        r = m.get(80, path)
        expect(r.status == 200, f"HTTP {r.status}")
        expect(r.headers["X-Origin-Count"] == n, f"request {n} answered from cache: {dict(r.headers)}")
        expect(r.headers["Set-Cookie"] == f"session={n}; Path=/", f"Set-Cookie {r.headers['Set-Cookie']!r}")
    return "both requests reached the origin"


def check_big_header(m: Mirror) -> str:
    r = m.get(80, f"/big-header?{uuid.uuid4().hex}")
    expect(r.status == 200, f"HTTP {r.status}")
    size = len(r.headers["X-Big"] or "")
    expect(size == 32 * 1024, f"X-Big header is {size} bytes")
    return f"{size}-byte header passed"


def check_browser(m: Mirror, shots: Path) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # Connect directly even if the environment sets an HTTP(S) proxy.
        browser = pw.chromium.launch(args=["--no-proxy-server"])
        try:
            page = browser.new_page()
            requests: list[str] = []
            failed: list[str] = []
            page.on("request", lambda req: requests.append(req.url))
            page.on("requestfailed", lambda req: failed.append(f"{req.url} ({req.failure})"))
            try:
                resp = page.goto(m.url(80), wait_until="load", timeout=30_000)
                expect(resp is not None and resp.status == 200, f"HTTP {resp.status if resp else None}")
                try:
                    page.wait_for_function(
                        "document.getElementById('status').textContent !== 'loading'", timeout=15_000
                    )
                except Exception:  # noqa: BLE001 - reported below with the failed requests
                    pass
                status = page.inner_text("#status")
                expect(status == "all loaded: cdn-ok", f"page status {status!r}, failed requests: {failed}")
                pixel = page.evaluate("document.getElementById('pixel').naturalWidth")
                expect(pixel == 1, "image from cdn.test did not load")
            finally:
                page.screenshot(path=str(shots / "browser.png"), full_page=True)
            hosts = {urlsplit(u).netloc for u in requests}
            foreign = sorted(h for h in hosts if urlsplit(f"//{h}").hostname != m.address)
            expect(not foreign, f"browser connected outside the mirror: {foreign}")
            return f"{len(requests)} requests, all to the mirror ({', '.join(sorted(hosts))})"
        finally:
            browser.close()


def check_nodeinfo(node: YggNode, public_key: str) -> str:
    info = node.ctl("getNodeInfo", f"key={public_key}")
    name = find_key(info, "name")
    expect(name == NODE_NAME, f"NodeInfo name {name!r}, expected {NODE_NAME!r}: {info}")
    return f"name {name!r}"


def find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = find_key(v, key)
            if found is not None:
                return found
    return None


def scenario_mirror(workdir: Path, artifacts: Path, timeout: int, keep: bool) -> list[Result]:
    private_key = ygg_genconf()["PrivateKey"]
    address, public_key = ygg_identity(private_key, workdir)
    log(f"Mirror: {address} (key {public_key})")
    node = YggNode(workdir, artifacts)
    dep = Deployment("e2e-mirror", workdir, artifacts, {
        "YGG_PRIV_KEY": private_key,
        "YGG_PEERS": f"tcp://host.docker.internal:{YGG_LISTEN_PORT}",
        "YGG_NODE_NAME": NODE_NAME,
        "TARGET_DOMAIN": TARGET_DOMAIN,
        "ADDITIONAL_DOMAINS": ADDITIONAL_DOMAINS,
    })
    m = Mirror(address)
    results: list[Result] = []
    try:
        node.start()
        dep.up()

        deadline = time.monotonic() + timeout
        log("Waiting for the mirror to answer over Yggdrasil")
        def reachable() -> str:
            status = m.get(80, "/links").status
            expect(status == 200, f"HTTP {status}")
            return f"HTTP {status} from {m.url(80)}"

        ready = retry("mirror reachable over Yggdrasil", reachable, deadline)
        results.append(ready)
        if not ready.ok:
            return results

        checks = []
        for u in UPSTREAMS:
            checks += [
                (f"upstream :{u.mirror_port}", check_upstream, (m, u)),
                (f"link rewriting :{u.mirror_port}", check_rewrite, (m, u)),
                (f"no real addresses :{u.mirror_port}", check_no_leaks, (m, u)),
                (f"redirect :{u.mirror_port}", check_redirect, (m, u)),
            ]
        checks += [
            (f"redirect :{UPSTREAMS[0].mirror_port} to {to.host}", check_redirect, (m, UPSTREAMS[0], to))
            for to in UPSTREAMS[1:]
        ]
        checks += [
            ("cache", check_cache, (m,)),
            ("cache separate per domain", check_cache_per_domain, (m,)),
            ("Set-Cookie not cached", check_set_cookie_not_cached, (m,)),
            ("large response headers", check_big_header, (m,)),
            ("browser", check_browser, (m, artifacts)),
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(once, name, lambda fn=fn, a=a: fn(*a)) for name, fn, a in checks]
            futures.append(pool.submit(
                retry, "NodeInfo name", lambda: check_nodeinfo(node, public_key), time.monotonic() + 60
            ))
            results += [f.result() for f in futures]
    except Exception as e:  # noqa: BLE001 - any setup failure fails the run
        results.append(Result("setup", False, describe(e)))
    finally:
        dep.dump_logs()
        if not keep:
            dep.down()
        node.stop()
    return results


# --- scenario: config --------------------------------------------------------


def config_env(**overrides: str | None) -> dict[str, str]:
    env = {
        "YGG_PRIV_KEY": ygg_genconf()["PrivateKey"],
        # nothing listens there: this scenario does not need connectivity
        "YGG_PEERS": "tcp://127.0.0.1:1 tls://127.0.0.1:2",
        "TARGET_DOMAIN": TARGET_DOMAIN,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def check_missing_var(workdir: Path, artifacts: Path, var: str) -> str:
    dep = Deployment(f"e2e-config-missing-{var.lower()}", workdir, artifacts, config_env(**{var: None}))
    p = dep.compose("config", "-q", check=False, capture_output=True)
    expect(p.returncode != 0, "docker compose config succeeded")
    expect(f"{var} is required" in p.stderr, f"unexpected error: {p.stderr.strip()!r}")
    return f"rejected: {p.stderr.strip().splitlines()[-1]}"


def check_proxy_rejects(workdir: Path, artifacts: Path, name: str, error: str, keep: bool, **env: str) -> str:
    """The proxy must refuse to start and log `error` (a regex)."""
    dep = Deployment(f"e2e-config-{name}", workdir, artifacts, config_env(**env))
    try:
        dep.up()

        def failed() -> str:
            logs = dep.logs("proxy")
            state = dep.container("proxy")["State"]
            expect(re.search(error, logs) is not None, f"no {error!r} in proxy logs")
            expect(state["ExitCode"] == 1, f"proxy state {state['Status']}, exit code {state['ExitCode']}")
            expect("Generated Nginx Config" not in logs, "nginx config generated anyway")
            return f"proxy exited with code 1: {re.search(error, logs)[0]!r}"

        return raise_on_fail(retry(name, failed, time.monotonic() + 60, interval=1))
    finally:
        dep.dump_logs()
        if not keep:
            dep.down()


def check_minimal(workdir: Path, artifacts: Path, keep: bool) -> str:
    env = config_env()
    dep = Deployment("e2e-config-minimal", workdir, artifacts, env)
    try:
        dep.up()

        def nginx() -> str:
            conf = dep.exec("proxy", "nginx", "-T")
            listens = re.findall(r"^\s*listen\s+([^;]*);", conf, re.M)
            expect(listens == ["[::]:80 default_server"], f"nginx listens on {listens}")
            upstreams = re.findall(r"^\s*proxy_pass\s+([^;]*);", conf, re.M)
            expect(upstreams == ["https://site.test:443"], f"proxy_pass {upstreams}")
            return "only [::]:80 -> https://site.test:443"

        def ygg() -> str:
            conf = json.loads(dep.exec(
                "yggdrasil", "yggdrasil", "-useconffile", "/etc/yggdrasil/yggdrasil.conf", "-normaliseconf", "-json"
            ))
            expect(conf["PrivateKey"] == env["YGG_PRIV_KEY"], "PrivateKey differs from YGG_PRIV_KEY")
            expect(conf["Peers"] == env["YGG_PEERS"].split(), f"Peers {conf['Peers']}")
            expect(conf.get("NodeInfo") in ({}, None), f"NodeInfo {conf.get('NodeInfo')} without YGG_NODE_NAME")
            return "PrivateKey and Peers from env, empty NodeInfo"

        deadline = time.monotonic() + 120
        return "; ".join(raise_on_fail(retry(n, fn, deadline, interval=1)) for n, fn in
                         (("nginx", nginx), ("yggdrasil", ygg)))
    finally:
        dep.dump_logs()
        if not keep:
            dep.down()


def raise_on_fail(r: Result) -> str:
    if not r.ok:
        raise CheckFailed(r.detail)
    return r.detail


def scenario_config(workdir: Path, artifacts: Path, timeout: int, keep: bool) -> list[Result]:
    del timeout  # every case has its own short deadline
    checks = [
        (f"missing {var}", lambda var=var: check_missing_var(workdir, artifacts, var))
        for var in ("YGG_PRIV_KEY", "YGG_PEERS", "TARGET_DOMAIN")
    ]
    checks += [
        ("TARGET_DOMAIN without scheme", lambda: check_proxy_rejects(
            workdir, artifacts, "target-no-scheme",
            # the quotes around the value are eaten by the outer sh -c '...' quoting
            r"Error: TARGET_DOMAIN '?site\.test'? is missing http:// or https:// scheme prefix\.", keep,
            TARGET_DOMAIN="site.test",
        )),
        ("ADDITIONAL_DOMAINS entry without scheme", lambda: check_proxy_rejects(
            workdir, artifacts, "additional-no-scheme",
            r"Error: Domain '?cdn\.test:8080'? in ADDITIONAL_DOMAINS is missing http:// or https:// scheme prefix\.",
            keep, ADDITIONAL_DOMAINS="https://api.test:8443 cdn.test:8080",
        )),
        ("minimal configuration", lambda: check_minimal(workdir, artifacts, keep)),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(once, name, fn) for name, fn in checks]
        return [f.result() for f in futures]


SCENARIOS = {"mirror": scenario_mirror, "config": scenario_config}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    ap.add_argument("--workdir", type=Path, help="working directory (default: temporary)")
    ap.add_argument("--artifacts", type=Path, help="logs and screenshots (default: <workdir>/artifacts)")
    ap.add_argument("--timeout", type=int, default=180, help="seconds for the mirror to come up")
    ap.add_argument("--keep", action="store_true", help="do not tear the deployments down")
    args = ap.parse_args()

    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix=f"e2e-{args.scenario}-"))).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    artifacts = (args.artifacts or workdir / "artifacts").resolve()
    artifacts.mkdir(parents=True, exist_ok=True)

    results = SCENARIOS[args.scenario](workdir, artifacts, args.timeout, args.keep)

    log("")
    for r in results:
        log(f"{'PASS' if r.ok else 'FAIL'}  {r.name}: {r.detail}")
    ok = bool(results) and all(r.ok for r in results)
    log(f"\n{'OK' if ok else 'FAILED'}: {sum(r.ok for r in results)}/{len(results)} checks passed; artifacts in {artifacts}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
