"""Fixture-only v1 to v2 migration. Not an operational migration interface."""
from __future__ import annotations

import sqlite3
import tempfile
import os
import shutil
import weakref
import sys
from pathlib import Path

from app.collateral_ledger import verify_collateral_invariants
from app.storage import CURRENT_SCHEMA_COLUMNS, _validate_schema_v1, connect, init_db, verify_audit_chain
from app.v2_clob_schema import INDEXES, TABLES, TRIGGERS, validate_v2_schema

_OLD = ('order_collateral_reservations', 'order_collateral_events', 'order_collateral_ledger_entries')
_NEW = ('v2_orders', 'v2_trades', 'v2_fills', 'v2_trade_ledger_entries', 'v2_command_results', 'v2_priority_sequence')
_FIXTURES = weakref.WeakKeyDictionary()
_HISTORY_MIGRATING = weakref.WeakSet()


class IsolatedMigrationFixture:
    """Own a fresh temporary database; accepting external paths is prohibited."""
    def __init__(self):
        self._directory = tempfile.TemporaryDirectory(prefix='clob-v2-fixture-')
        self.path = Path(self._directory.name) / 'fixture.sqlite'
        self.conn = connect(str(self.path))
        init_db(self.conn)
        # Keep SQLite temporary btrees in memory; all persistent files stay on the fixture filesystem.
        self.conn.execute('PRAGMA temp_store=MEMORY')
        _FIXTURES[self] = (self._directory, self.path, self.conn, (self.path.stat().st_dev, self.path.stat().st_ino))
        self._connection_identity = id(self.conn)
        self._file_identity = (self.path.stat().st_dev, self.path.stat().st_ino)

    def close(self):
        _FIXTURES.pop(self, None)
        self.conn.close()
        self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _old_rows(conn):
    return {table: [tuple(row) for row in conn.execute(
        f"SELECT {','.join(CURRENT_SCHEMA_COLUMNS[table])} FROM {table} ORDER BY id")]
        for table in _OLD}


def _audit(conn):
    result = verify_audit_chain(conn)
    if result['integrity_status'] not in ('verified', 'empty'):
        raise RuntimeError('audit_invalid')
    row = conn.execute('SELECT event_hash FROM demo_audit_events ORDER BY id DESC LIMIT 1').fetchone()
    return row[0] if row else None


def _check_v1(conn):
    _validate_schema_v1(conn)
    reference = connect(':memory:')
    try:
        init_db(reference)
        for table in _OLD:
            query = "select sql from sqlite_master where type='table' and name=?"
            actual = conn.execute(query, (table,)).fetchone()
            expected = reference.execute(query, (table,)).fetchone()
            if actual is None or tuple(actual) != tuple(expected):
                raise RuntimeError('legacy_schema_invalid')
            query = "select type,name,sql from sqlite_master where tbl_name=? and type in ('index','trigger') and sql is not null order by type,name"
            if [tuple(row) for row in conn.execute(query, (table,))] != [tuple(row) for row in reference.execute(query, (table,))]:
                raise RuntimeError('legacy_schema_invalid')
        for row in conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"):
            name = row[0]
            if name not in _OLD:
                escaped = name.replace('"', '""')
                if any(fk[2] in _OLD for fk in conn.execute(f'pragma foreign_key_list("{escaped}")')):
                    raise RuntimeError('unsupported_foreign_key_dependency')
    finally:
        reference.close()
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('integrity_invalid')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RuntimeError('foreign_key_invalid')
    _audit(conn)
    if verify_collateral_invariants(conn)['integrity_status'] != 'verified':
        raise RuntimeError('collateral_invalid')


def _check_capacity(conn, path):
    """Check measured space needs; free space can still change after this check.

    A snapshot needs the live page footprint. Shadow tables and their indexes
    are estimated from that same measured footprint, and a rollback journal
    is budgeted for the original pages. Existing WAL/journal bytes are included.
    Temporary btrees are required to be in memory for this fixture-only path.
    """
    if conn.execute('PRAGMA temp_store').fetchone()[0] != 2:
        raise RuntimeError('fixture_temp_store_invalid')
    if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist', 'wal'):
        raise RuntimeError('fixture_journal_mode_invalid')
    pages = conn.execute('PRAGMA page_count').fetchone()[0]
    page_size = conn.execute('PRAGMA page_size').fetchone()[0]
    if type(pages) is not int or type(page_size) is not int or pages <= 0 or page_size <= 0:
        raise RuntimeError('capacity_unavailable')
    footprint = pages * page_size
    try:
        sidecars = sum(Path(str(path) + suffix).stat().st_size
                       for suffix in ('-wal', '-journal') if Path(str(path) + suffix).exists())
        free = shutil.disk_usage(path.parent).free
        temp_dir = Path(tempfile.gettempdir())
        temp_free = shutil.disk_usage(temp_dir).free
        same_filesystem = path.parent.stat().st_dev == temp_dir.stat().st_dev
    except OSError as exc:
        raise RuntimeError('capacity_unavailable') from exc
    # Snapshot, rebuilt objects, rollback journal and conservative temp allowance.
    required = footprint + footprint + footprint + sidecars
    if free < required + (footprint if same_filesystem else 0) or (not same_filesystem and temp_free < footprint):
        raise RuntimeError('capacity_insufficient')


def _legacy_table_columns(conn):
    tables=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name:[row[1] for row in conn.execute('PRAGMA table_info("'+name.replace('"','""')+'")')] for name in tables}


def _legacy_table_rows(conn, columns):
    from collections import Counter
    result={}
    for name,names in columns.items():
        selected=','.join('"'+item.replace('"','""')+'"' for item in names)
        result[name]=Counter(tuple(row) for row in conn.execute('SELECT '+selected+' FROM "'+name.replace('"','""')+'"'))
    return result


def _history_migration_authorized(fixture):
    registered = _FIXTURES.get(fixture)
    if type(fixture) is not IsolatedMigrationFixture or registered is None or fixture not in _HISTORY_MIGRATING:
        return False
    directory, expected, conn, identity = registered
    try:
        db_path=conn.execute('PRAGMA database_list').fetchone()[2]
        return (fixture._directory is directory and fixture.path == expected and fixture.conn is conn
                and expected == Path(directory.name) / 'fixture.sqlite' and not expected.is_symlink()
                and Path(db_path).resolve() == expected.resolve()
                and (expected.stat().st_dev, expected.stat().st_ino) == identity and expected.stat().st_nlink == 1
                and conn.in_transaction and conn.execute('PRAGMA foreign_keys').fetchone()[0] == 0
                and conn.execute('PRAGMA user_version').fetchone()[0] == 2)
    except (OSError,sqlite3.Error):
        return False


def _history_migration_connection_authorized(conn):
    return any(fixture.conn is conn and _history_migration_authorized(fixture) for fixture in list(_HISTORY_MIGRATING))


def migrate_fixture(fixture: IsolatedMigrationFixture, *, failure_hook=None, with_history=False):
    """Migrate only a database freshly created and exclusively owned by fixture.

    This deliberately has no user-supplied path or production invocation path.
    The caller is responsible for ensuring the fixture has no other connections.
    """
    if type(with_history) is not bool:
        raise TypeError('with_history_must_be_bool')
    if type(fixture) is not IsolatedMigrationFixture:
        raise TypeError('fixture_required')
    registered = _FIXTURES.get(fixture)
    if registered is None:
        raise RuntimeError('fixture_ownership_invalid')
    directory, expected, conn, identity = registered
    if fixture._directory is not directory or fixture.path != expected or fixture.conn is not conn:
        raise RuntimeError('fixture_ownership_invalid')
    database_path = conn.execute('PRAGMA database_list').fetchone()[2]
    if (expected != Path(directory.name) / 'fixture.sqlite' or expected.is_symlink()
            or Path(database_path).resolve() != expected.resolve()
            or (expected.stat().st_dev, expected.stat().st_ino) != identity
            or expected.stat().st_nlink != 1):
        raise RuntimeError('fixture_ownership_invalid')
    if conn.in_transaction:
        raise RuntimeError('schema migration requires clean connection')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise RuntimeError('foreign_keys_required')
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version == 2:
        validate_v2_schema(conn)
        history_present = conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'balance_history_%' LIMIT 1").fetchone() is not None
        if with_history or history_present:
            from app.v2_balance_history import verify_history
            if verify_history(conn)['integrity_status'] != 'verified':
                raise RuntimeError('history_invalid')
        return False
    if version != 1:
        raise RuntimeError('unsupported schema version')
    _check_v1(conn)
    _check_capacity(conn, expected)
    # An active SQLite writer must be rejected before snapshot or DDL. This is
    # fixture concurrency control, not proof of operational writer exclusion.
    conn.execute('BEGIN IMMEDIATE')
    conn.rollback()
    before = _old_rows(conn)
    audit_before = _audit(conn)
    sequence = dict(conn.execute("SELECT name,seq FROM sqlite_sequence WHERE name IN (?,?,?)", _OLD).fetchall())
    legacy_columns = _legacy_table_columns(conn) if with_history else None
    legacy_rows = _legacy_table_rows(conn, legacy_columns) if with_history else None
    legacy_sequences = dict(conn.execute('SELECT name,seq FROM sqlite_sequence')) if with_history else None
    # Snapshot and readback use a newly created, private fixture path only.
    fd, snapshot_name = tempfile.mkstemp(prefix='before-', suffix='.sqlite', dir=fixture._directory.name)
    os.close(fd)
    snapshot = Path(snapshot_name)
    with sqlite3.connect(snapshot) as snap:
        conn.backup(snap)
        if snap.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or snap.execute('PRAGMA foreign_key_check').fetchone() is not None or snap.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise RuntimeError('snapshot_invalid')
    hook = failure_hook or (lambda stage: None)
    conn.execute('PRAGMA foreign_keys=OFF')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
        raise RuntimeError('foreign_keys_disable_failed')
    try:
        conn.execute('BEGIN IMMEDIATE')
        # Shadow names are new; children always reference canonical targets.
        for table in _OLD:
            conn.execute(TABLES[table].replace('CREATE TABLE '+table+' (', 'CREATE TABLE '+table+'_v2_shadow (', 1))
        hook('shadows')
        for table in _OLD:
            columns = ','.join(CURRENT_SCHEMA_COLUMNS[table])
            if table == _OLD[0]:
                extras = ',contract_version,remaining_collateral,consumed_collateral,released_collateral,consumed_at'
                values = ",1,CASE WHEN status='reserved' THEN collateral_amount ELSE 0 END,0,CASE WHEN status='released' THEN collateral_amount ELSE 0 END,NULL"
            else:
                extras = values = ''
            conn.execute(f'INSERT INTO {table}_v2_shadow ({columns}{extras}) SELECT {columns}{values} FROM {table}')
            hook('copy_'+table)
        for table in reversed(_OLD):
            conn.execute('DROP TABLE '+table)
        hook('drop_old')
        for table in _OLD:
            conn.execute('ALTER TABLE '+table+'_v2_shadow RENAME TO '+table)
        hook('rename')
        for table in _NEW:
            conn.execute(TABLES[table])
        for sql in INDEXES.values():
            conn.execute(sql)
        for sql in TRIGGERS.values():
            conn.execute(sql)
        conn.execute('INSERT INTO v2_priority_sequence VALUES (1,0)')
        for table, old_seq in sequence.items():
            row = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
            if row is None:
                conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', (table, old_seq))
            elif row[0] < old_seq:
                conn.execute('UPDATE sqlite_sequence SET seq=? WHERE name=?', (old_seq, table))
        hook('indexes')
        conn.execute('PRAGMA user_version=2')
        hook('precommit')
        validate_v2_schema(conn)
        if _old_rows(conn) != before or _audit(conn) != audit_before:
            raise RuntimeError('legacy_data_changed')
        for table, old_seq in sequence.items():
            row = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
            if row is None or row[0] < old_seq:
                raise RuntimeError('legacy_sequence_changed')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('foreign_key_invalid')
        # V2 reconciliation also includes legacy reservations and their ledgers.
        if verify_collateral_invariants(conn)['integrity_status'] != 'verified':
            raise RuntimeError('collateral_invalid')
        if with_history:
            from app.v2_balance_history import _install_checkpoint_for_migration, verify_history
            _HISTORY_MIGRATING.add(fixture)
            try:
                _install_checkpoint_for_migration(fixture, failure_hook=hook)
                hook('history_checkpoint')
                if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise RuntimeError('foreign_key_invalid')
                if verify_history(conn)['integrity_status'] != 'verified':
                    raise RuntimeError('history_invalid')
                hook('history_precommit')
                if conn.execute('PRAGMA foreign_key_check').fetchone() is not None or verify_history(conn)['integrity_status'] != 'verified':
                    raise RuntimeError('history_invalid')
                if _legacy_table_rows(conn,legacy_columns)!=legacy_rows or _audit(conn)!=audit_before:
                    raise RuntimeError('legacy_data_changed')
                current_sequences=dict(conn.execute('SELECT name,seq FROM sqlite_sequence'))
                if any(current_sequences.get(name,-1)<value for name,value in legacy_sequences.items()):
                    raise RuntimeError('legacy_sequence_changed')
            finally:
                _HISTORY_MIGRATING.discard(fixture)
        conn.commit()
    except Exception:
        if conn.in_transaction:
            try:
                conn.rollback()
            except sqlite3.Error:
                # Do not leave a failed transaction available for a later commit.
                try:
                    conn.close()
                except sqlite3.Error:
                    pass  # Preserve the original SQLite failure.
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise RuntimeError('foreign_keys_restore_failed')
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('foreign_key_invalid')
        except Exception:
            if not active_error:
                raise
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RuntimeError('foreign_key_invalid')
    return True
