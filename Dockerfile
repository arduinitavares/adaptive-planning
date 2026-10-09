FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS sqlite-builder

# The pinned Debian library lacks the upstream WAL-reset fix. Build the
# verified normal amalgamation; compiler/source never enter the runtime stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y gcc libc6-dev \
    && mkdir /sqlite-source /sqlite-library \
    && python -c "import hashlib, io, tarfile, urllib.request; response = urllib.request.urlopen('https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz', timeout=30); data = response.read(3283178); response.close(); assert len(data) == 3283177; assert hashlib.sha3_256(data).hexdigest() == '454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338'; assert hashlib.sha256(data).hexdigest() == '0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c'; archive = tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); archive.extractall('/sqlite-source', filter='data'); archive.close()" \
    && gcc -O2 -fPIC -shared -Wl,-soname,libsqlite3.so.0 -DSQLITE_THREADSAFE=1 \
       /sqlite-source/sqlite-autoconf-3530400/sqlite3.c \
       -o /sqlite-library/libsqlite3.so.0 -lpthread -ldl -lm

FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

COPY --from=sqlite-builder /sqlite-library/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
# Prefer the owned library before the Debian multiarch ldconfig cache entries.
ENV LD_LIBRARY_PATH=/usr/local/lib
RUN ldconfig \
    && python -c "import sqlite3, sys, tempfile; from pathlib import Path; sqlite_libraries = {line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines() if 'libsqlite3.so' in line}; assert sqlite_libraries == {'/usr/local/lib/libsqlite3.so.0'}, sqlite_libraries; assert sys.version_info[:3] == (3, 13, 16); assert sqlite3.sqlite_version == '3.53.4'; assert sqlite3.threadsafety == 3; temporary = tempfile.TemporaryDirectory(); db = sqlite3.connect(temporary.name + '/sqlite-runtime-probe.sqlite'); source_id = db.execute('SELECT sqlite_source_id()').fetchone()[0]; assert source_id == '2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc'; assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',); db.execute('PRAGMA foreign_keys=ON'); assert db.execute('PRAGMA foreign_keys').fetchone() == (1,); assert db.execute(\"SELECT json_valid('{}')\").fetchone() == (1,); db.execute('CREATE TABLE probe(value INTEGER)'); db.execute('INSERT INTO probe VALUES(23)'); db.commit(); target = sqlite3.connect(':memory:'); db.backup(target); assert target.execute('SELECT value FROM probe').fetchone() == (23,); target.close(); print({'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'loaded_sqlite_libraries': sorted(sqlite_libraries), 'source_id': source_id, 'threadsafety': sqlite3.threadsafety, 'compile_options': [row[0] for row in db.execute('PRAGMA compile_options')]}); db.close(); temporary.cleanup()"

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADAPTIVE_PLANNING_DATA_DIR=/data \
    ADAPTIVE_PLANNING_BASE_URL=http://127.0.0.1:8790

WORKDIR /app
COPY pyproject.toml uv.lock /app/
COPY adaptive_planning /app/adaptive_planning
COPY assets/dashboard /app/assets/dashboard

RUN uv sync --frozen --no-dev --no-editable \
    && install -d -m 0700 -o 10001 -g 10001 /data

USER 10001:10001
EXPOSE 8790
ENTRYPOINT ["/app/.venv/bin/adaptive-planning"]
CMD ["serve"]
