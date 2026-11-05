import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.tx() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records (
                  pk INTEGER PRIMARY KEY, tenant TEXT NOT NULL, source TEXT NOT NULL,
                  kind TEXT NOT NULL, source_id TEXT NOT NULL, parent TEXT,
                  revision INTEGER NOT NULL, po_number TEXT, body TEXT NOT NULL,
                  UNIQUE(tenant,source,kind,source_id));
                CREATE UNIQUE INDEX IF NOT EXISTS po_number_unique
                  ON records(tenant,source,po_number) WHERE kind='po';
                CREATE TABLE IF NOT EXISTS cursors (
                  tenant TEXT NOT NULL, source TEXT NOT NULL,
                  seq INTEGER NOT NULL DEFAULT 0, checked TEXT, gap_target INTEGER,
                  PRIMARY KEY(tenant,source));
                CREATE TABLE IF NOT EXISTS issues (
                  tenant TEXT NOT NULL, source TEXT NOT NULL, code TEXT NOT NULL,
                  scope TEXT NOT NULL, detail TEXT NOT NULL, since TEXT NOT NULL,
                  PRIMARY KEY(tenant,source,code));
                CREATE TABLE IF NOT EXISTS alerts (
                  id INTEGER PRIMARY KEY, tenant TEXT, source TEXT, code TEXT,
                  state TEXT, created TEXT, delivered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS operations (
                  op_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, source TEXT NOT NULL,
                  line_id TEXT NOT NULL, po_id TEXT NOT NULL, eta TEXT NOT NULL,
                  base_rev INTEGER NOT NULL, po_rev INTEGER NOT NULL,
                  state TEXT NOT NULL, remote_rev INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS pending_line
                  ON operations(tenant,source,line_id)
                  WHERE state IN ('unknown','confirmed');
                CREATE INDEX IF NOT EXISTS issues_by_scope
                  ON issues(tenant,source,scope);
            ''')

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def one(db, sql, args=()):
    return db.execute(sql, args).fetchone()


def record(db, tenant, source, kind, source_id):
    return one(db, '''SELECT * FROM records WHERE tenant=? AND source=?
                  AND kind=? AND source_id=?''', (tenant, source, kind, str(source_id)))


def flag(db, tenant, source, code, scope, detail):
    old = one(db, 'SELECT scope,detail FROM issues WHERE tenant=? AND source=? AND code=?',
              (tenant, source, code))
    db.execute('''INSERT INTO issues VALUES(?,?,?,?,?,?)
               ON CONFLICT(tenant,source,code) DO UPDATE SET
               scope=excluded.scope, detail=excluded.detail''',
               (tenant, source, code, scope, detail, now()))
    if not old or (old['scope'], old['detail']) != (scope, detail):
        db.execute('INSERT INTO alerts(tenant,source,code,state,created) VALUES(?,?,?,?,?)',
                   (tenant, source, code, 'opened', now()))


def clear(db, tenant, source, code):
    if db.execute('DELETE FROM issues WHERE tenant=? AND source=? AND code=?',
                  (tenant, source, code)).rowcount:
        db.execute('INSERT INTO alerts(tenant,source,code,state,created) VALUES(?,?,?,?,?)',
                   (tenant, source, code, 'resolved', now()))


def blocked(db, tenant, source, po_id):
    return one(db, '''SELECT 1 FROM issues WHERE tenant=? AND source=?
                  AND scope IN ('*',?) LIMIT 1''', (tenant, source, 'po:' + str(po_id))) is not None


def notify_pending(store, send):
    """At-least-once notification dispatch. Receiver deduplicates by alert.id."""
    with store.tx() as db:
        pending = [dict(r) for r in db.execute('SELECT * FROM alerts WHERE delivered=0 ORDER BY id')]
    for alert in pending:
        try:
            send(alert)  # e.g. Slack, PagerDuty, email or internal incident bus
        except Exception:
            break
        with store.tx() as db:
            db.execute('UPDATE alerts SET delivered=1 WHERE id=?', (alert['id'],))
