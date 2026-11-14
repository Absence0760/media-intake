"""Static guard for the "nothing leaves the machine" rule (CLAUDE.md, README).

Reads the package source with `ast` and fails when a change:
  - imports a network client anywhere except vlm_trash.py, the one module
    allowed to talk to Ollama (and only on 127.0.0.1, which its own tests check);
  - starts an HTTP server bound to anything but the literal "127.0.0.1";
  - shells out to a downloader (curl, wget) at run time; downloads belong in
    setup.sh, pinned by sha256.

A new legitimate use goes through review by changing the allow-list here.
"""
import ast, glob, os, unittest

PKG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "media_intake")

NET_TOP = {"socket", "ssl", "requests", "httpx", "aiohttp", "urllib3", "ftplib", "smtplib",
           "poplib", "imaplib", "telnetlib", "websocket", "websockets", "paramiko"}
NET_FULL = {"urllib.request", "http.client", "xmlrpc.client"}
# module file -> network modules it may import
ALLOWED = {"vlm_trash.py": {"urllib.request"}}
DOWNLOADERS = {"curl", "wget", "aria2c"}


def modules():
    for path in sorted(glob.glob(os.path.join(PKG, "*.py"))):
        with open(path, encoding="utf-8") as fh:
            yield os.path.basename(path), ast.parse(fh.read(), path)


def imported(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from (a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            yield n.module


def call_name(node):
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


class OfflineGuard(unittest.TestCase):
    def test_network_imports_only_where_allowed(self):
        bad = []
        for name, tree in modules():
            for m in imported(tree):
                if (m.split(".")[0] in NET_TOP or m in NET_FULL) and m not in ALLOWED.get(name, set()):
                    bad.append(f"{name}: import {m}")
        self.assertEqual(bad, [], "network client outside the allow-list (nothing leaves the machine)")

    def test_http_servers_bind_loopback(self):
        bad, seen = [], 0
        for name, tree in modules():
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and call_name(n).endswith("HTTPServer") and n.args:
                    seen += 1
                    addr = n.args[0]
                    host = addr.elts[0] if isinstance(addr, ast.Tuple) and addr.elts else None
                    if not (isinstance(host, ast.Constant) and host.value == "127.0.0.1"):
                        bad.append(f"{name}:{n.lineno}")
        self.assertGreater(seen, 0, "found no HTTP server at all: the guard is not matching")
        self.assertEqual(bad, [], "HTTP server not bound to the literal 127.0.0.1")

    def test_no_runtime_downloaders(self):
        bad = []
        for name, tree in modules():
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in DOWNLOADERS:
                    bad.append(f"{name}:{n.lineno} {n.value!r}")
        self.assertEqual(bad, [], "downloads belong in setup.sh, pinned by sha256")


if __name__ == "__main__":
    unittest.main()
