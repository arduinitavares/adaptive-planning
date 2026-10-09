"""Installed distribution behavior and the persistent loopback container contract."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "app.js", "style.css")
PYTHON_IMAGE = (
    "python:3.13.16-slim-bookworm@sha256:"
    "a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
)


def environment() -> dict[str, str]:
    """Keep test children independent of local project/provider configuration."""
    result = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TMPDIR", "UV_PYTHON") if name in os.environ}
    result.update(UV_NO_CONFIG="1", UV_PYTHON_DOWNLOADS="never", PYTHONDONTWRITEBYTECODE="1")
    return result


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(argv, cwd=cwd, env=env or environment(), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if process.returncode:
        raise AssertionError(f"Command failed ({process.returncode}): {argv!r}\n{process.stdout}")
    return process.stdout


INSTALLED_PROBE = textwrap.dedent(r'''
    import asyncio
    import hashlib
    import importlib
    import importlib.metadata
    from importlib import resources
    import json
    import os
    from pathlib import Path
    import pkgutil
    import sys
    import threading

    checkout, venv = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    expected = json.loads(sys.argv[3])
    working = Path.cwd().resolve()
    assert not working.is_relative_to(checkout), "Probe must run outside the checkout"
    before_files = set(working.rglob("*"))
    before_threads = set(threading.enumerate())
    importing = True

    def forbid_import_side_effects(event, arguments):
        if importing and event in {"sqlite3.connect", "socket.__new__"}:
            raise AssertionError("Module import attempted a database or socket side effect")

    sys.addaudithook(forbid_import_side_effects)
    import adaptive_planning
    for module in pkgutil.iter_modules(adaptive_planning.__path__, adaptive_planning.__name__ + "."):
        importlib.import_module(module.name)
    importing = False
    package = Path(adaptive_planning.__file__).resolve()
    assert package.is_relative_to(venv), "Imported package is not installed in the temporary environment"
    assert not package.is_relative_to(checkout), "Checkout package accidentally satisfied the test"
    assert set(working.rglob("*")) == before_files, "Import created files or a data directory"
    assert set(threading.enumerate()) == before_threads, "Import started a background thread"
    assert not (package.parent.parent / "assets" / "dashboard").exists(), "Checkout asset fallback is available"
    entries = importlib.metadata.distribution("adaptive-planning").entry_points
    assert any(entry.group == "console_scripts" and entry.name == "adaptive-planning"
               and entry.value == "adaptive_planning.__main__:main" for entry in entries), "CLI entry is missing"

    dashboard = resources.files("adaptive_planning").joinpath("assets", "dashboard")
    for name, digest in expected.items():
        asset = dashboard.joinpath(name)
        assert asset.is_file(), f"Missing installed dashboard resource: {name}"
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == digest, f"Installed resource differs: {name}"

    from adaptive_planning.admin import initialize_installation
    from adaptive_planning.app import build_app
    from adaptive_planning.config import Config
    config = Config(Path(os.environ["ADAPTIVE_PLANNING_DATA_DIR"]))
    initialize_installation(config)
    app = build_app(config)

    async def get(path):
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "scheme": "http", "method": "GET", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"127.0.0.1:8790")],
                 "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8790)}
        await app(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages
                        if message["type"] == "http.response.body")
        return start["status"], dict(start["headers"]), body

    for name, content_type in (("app.js", b"text/javascript"), ("style.css", b"text/css")):
        status, headers, body = asyncio.run(get("/" + name))
        assert status == 200, f"Installed asset route failed: {name}"
        assert headers[b"content-type"].startswith(content_type), f"Wrong asset content type: {name}"
        assert hashlib.sha256(body).hexdigest() == expected[name], f"Asset route returned wrong bytes: {name}"
    status, headers, body = asyncio.run(get("/"))
    assert status == 200 and b'<script src="/app.js"' in body, "Installed dashboard index failed"
    app.state.close()
    print("Installed resources, real asset routes, CLI entry, and import safety passed.")
''')


class TestService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory(prefix="adaptive-planning-packaging-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.uv = shutil.which("uv")
        if cls.uv is None:
            raise AssertionError("Packaging verification requires uv")
        cls.artifacts = cls.root / "artifacts"
        run([cls.uv, "build", "--out-dir", str(cls.artifacts)], cwd=ROOT)
        cls.requirements = cls.root / "requirements.txt"
        run([cls.uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements.txt",
             "--output-file", str(cls.requirements)], cwd=ROOT)
        cls.expected = {name: hashlib.sha256((ROOT / "assets" / "dashboard" / name).read_bytes()).hexdigest()
                        for name in ASSETS}

    def installed_probe(self, suffix: str):
        artifacts = list(self.artifacts.glob("*" + suffix))
        self.assertEqual(len(artifacts), 1, f"Expected one built {suffix} artifact")
        target = self.root / ("sdist-env" if suffix == ".tar.gz" else "wheel-env")
        work = self.root / (target.name + "-working")
        work.mkdir()
        run([self.uv, "venv", "--no-project", "--python", sys.executable, str(target)], cwd=work)
        executable = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([self.uv, "pip", "sync", "--python", str(executable), "--require-hashes", str(self.requirements)],
            cwd=work)
        run([self.uv, "pip", "install", "--python", str(executable), "--no-deps", str(artifacts[0])], cwd=work)
        env = environment()
        env["ADAPTIVE_PLANNING_DATA_DIR"] = str(work / "data")
        result = run([str(executable), "-I", "-B", "-c", INSTALLED_PROBE,
                      str(ROOT), str(target), json.dumps(self.expected)], cwd=work, env=env)
        self.assertIn("import safety passed", result)
        help_output = run([str(executable), "-I", "-B", "-m", "adaptive_planning", "--help"], cwd=work, env=env)
        self.assertIn("{init,serve}", help_output)

    def test_wheel_assets_and_import_safety(self):
        # Omitting wheel resource inclusion must fail without checkout fallback.
        self.installed_probe(".whl")

    def test_sdist_assets_and_import_safety(self):
        # Omitting assets from the sdist must break a fresh sdist installation too.
        self.installed_probe(".tar.gz")

    def test_container_sqlite_runtime_is_pinned_verified_and_feature_complete(self):
        # A base tag alone does not qualify its dynamically loaded SQLite.
        recipe=(ROOT/'Dockerfile').read_text()
        self.assertIn('FROM '+PYTHON_IMAGE+' AS sqlite-builder',recipe)
        self.assertIn('https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz',recipe)
        self.assertIn('454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338',recipe)
        self.assertIn('0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c',recipe)
        self.assertIn('hashlib.sha3_256',recipe)
        self.assertLess(recipe.index('hashlib.sha3_256'),recipe.index('extractall'))
        self.assertIn('-DSQLITE_THREADSAFE=1',recipe)
        for flag in ('-shared','-fPIC','-Wl,-soname,libsqlite3.so.0','-lpthread','-ldl','-lm'):
            self.assertIn(flag,recipe)
        self.assertNotIn('SQLITE_OMIT_',recipe)
        runtime=recipe.split('\nFROM '+PYTHON_IMAGE+'\n')[-1]
        self.assertNotIn('apt-get',runtime)
        self.assertNotIn('gcc ',runtime)
        self.assertIn('ldconfig',runtime)
        self.assertIn("sqlite3.sqlite_version == '3.53.4'",runtime)
        self.assertIn('2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc',runtime)
        self.assertIn('sqlite3.threadsafety == 3',runtime)
        self.assertIn('PRAGMA foreign_keys=ON',runtime)
        self.assertIn('PRAGMA journal_mode=WAL',runtime)
        self.assertIn('.backup(',runtime)

    def test_container_selects_owned_sqlite_and_proves_actual_loaded_path(self):
        # A verified DSO on disk does not mean Python's loader selects it.
        runtime=(ROOT/'Dockerfile').read_text().split('\nFROM '+PYTHON_IMAGE+'\n')[-1]
        environment={}
        probes=[]
        for line in runtime.replace('\\\n',' ').splitlines():
            instruction,_,value=line.strip().partition(' ')
            if instruction=='ENV':
                environment.update(item.split('=',1) for item in shlex.split(value))
            elif instruction=='RUN':
                words=shlex.split(value)
                if 'python' in words and '-c' in words:
                    self.assertEqual(environment.get('LD_LIBRARY_PATH'),'/usr/local/lib',
                                     'Python must select the owned SQLite ahead of Debian ldconfig entries')
                    probes.append(words[words.index('-c')+1])
        self.assertEqual(environment.get('LD_LIBRARY_PATH'),'/usr/local/lib',
                         'The owned-library selection must persist in the final runtime')
        self.assertEqual(len(probes),1)
        self.assertIn('/proc/self/maps',probes[0])
        assertions=[node.test for node in ast.walk(ast.parse(probes[0])) if isinstance(node,ast.Assert)]
        pinned_path=ast.parse("sqlite_libraries == {'/usr/local/lib/libsqlite3.so.0'}",mode='eval').body
        self.assertIn(ast.dump(pinned_path),[ast.dump(node) for node in assertions],
                      'The build probe must reject Python loading a different SQLite DSO')

    def test_container_contract(self):
        # A widened port/mount/credential boundary must fail before container activation.
        self.assertTrue((ROOT / "compose.yaml").is_file(), "Persistent loopback Compose configuration is missing")
        self.assertTrue((ROOT / "Dockerfile").is_file(), "Non-root container definition is missing")
        config = json.loads(run(["docker", "compose", "-f", str(ROOT / "compose.yaml"),
                                 "config", "--format", "json"], cwd=ROOT))
        self.assertEqual(set(config["services"]), {"adaptive-planning"})
        service = config["services"]["adaptive-planning"]
        self.assertEqual(service["user"], "10001:10001")
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertEqual(service["command"], ["serve"])
        self.assertEqual(service["environment"], {
            "ADAPTIVE_PLANNING_DATA_DIR": "/data",
            "ADAPTIVE_PLANNING_BASE_URL": "http://127.0.0.1:8790",
        })
        self.assertFalse(service.get("env_file"))
        ports = service["ports"]
        self.assertEqual(len(ports), 1)
        self.assertEqual((ports[0]["host_ip"], str(ports[0]["published"]), ports[0]["target"], ports[0]["protocol"]),
                         ("127.0.0.1", "8790", 8790, "tcp"))
        mounts = service["volumes"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual((mounts[0]["type"], mounts[0]["source"], mounts[0]["target"]),
                         ("volume", "adaptive-planning-data", "/data"))
        self.assertFalse(mounts[0].get("read_only", False))
        self.assertEqual(set(config["volumes"]), {"adaptive-planning-data"})
        self.assertEqual(config["volumes"]["adaptive-planning-data"]["name"], "adaptive-planning-data")
        for forbidden in ("privileged", "network_mode", "pid", "ipc", "devices", "volumes_from", "secrets", "configs"):
            self.assertFalse(service.get(forbidden), f"Unexpected host/control configuration: {forbidden}")

        instructions = []
        logical = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
        for line in logical.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                instruction, _, value = line.strip().partition(" ")
                instructions.append((instruction.upper(), value.strip()))
        self.assertIn(("FROM", PYTHON_IMAGE), instructions)
        self.assertIn(("USER", "10001:10001"), instructions)
        self.assertIn(("ENTRYPOINT", '["/app/.venv/bin/adaptive-planning"]'), instructions)
        self.assertIn(("CMD", '["serve"]'), instructions)
        commands = [shlex.split(value) for name, value in instructions if name == "RUN"]
        self.assertTrue(any(command[:2] == ["uv", "sync"] and {"--frozen", "--no-dev", "--no-editable"}.issubset(command)
                            for command in commands), "Image must install the frozen production package")
        self.assertFalse(any("init" in command for command in commands), "Build must never initialize credentials")
        copies = [shlex.split(value) for name, value in instructions if name == "COPY"]
        uv_copy = [copy for copy in copies if copy[0].startswith("--from=ghcr.io/")]
        self.assertEqual(uv_copy, [["--from=ghcr.io/astral-sh/uv:0.10.12", "/uv", "/usr/local/bin/uv"]])
        owned_copy = [copy for copy in copies if copy[0].startswith("--from=") and not copy[0].startswith("--from=ghcr.io/")]
        self.assertEqual(owned_copy, [["--from=sqlite-builder", "/sqlite-library/libsqlite3.so.0", "/usr/local/lib/libsqlite3.so.0"]])
        self.assertEqual([value for name,value in instructions if name=='FROM'],[PYTHON_IMAGE+' AS sqlite-builder',PYTHON_IMAGE])
        sources = {source for copy in copies if not copy[0].startswith("--from=") for source in copy[:-1]}
        self.assertEqual(sources, {"pyproject.toml", "uv.lock", "adaptive_planning", "assets/dashboard"})
        docker_environment = [item for name, value in instructions if name == "ENV" for item in shlex.split(value)]
        self.assertFalse(any("TOKEN" in item.upper() or "SECRET" in item.upper() for item in docker_environment))
        ignores = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(ignores[0], "**", "Build context must exclude local state by default")
        allowed = {line for line in ignores if line.startswith("!")}
        self.assertEqual(allowed, {"!Dockerfile", "!.dockerignore", "!pyproject.toml", "!uv.lock",
                                   "!adaptive_planning/", "!adaptive_planning/*.py", "!assets/",
                                   "!assets/dashboard/", "!assets/dashboard/index.html",
                                   "!assets/dashboard/app.js", "!assets/dashboard/style.css"})


if __name__ == "__main__":
    unittest.main()
