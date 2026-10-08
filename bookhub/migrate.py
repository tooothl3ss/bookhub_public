"""Copy the two legacy stores into a new destination. Source files are never changed.

Run only with legacy writers stopped. Failure leaves the staging directory for inspection,
but never exposes it as the destination. Re-running cannot overwrite an existing migration.
"""
import argparse
from contextlib import contextmanager
import tempfile
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time


@contextmanager
def readonly(path):
    # Even mode=ro may create a WAL shared-memory file. Work on a private copy so
    # a genuinely read-only source mount needs no sidecar writes or checkpoint.
    # Legacy writers must already be stopped, including while copying the WAL.
    with tempfile.TemporaryDirectory(prefix='bookhub-sqlite-copy-') as directory:
        snapshot = Path(directory)/path.name
        for suffix in ('', '-wal'):
            source = Path(str(path)+suffix)
            if source.exists():
                before = source.stat()
                shutil.copyfile(source,Path(str(snapshot)+suffix))
                after = source.stat()
                if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                    raise RuntimeError('Legacy database changed during copy; stop all writers')
        db = sqlite3.connect(snapshot)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def migrate(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise RuntimeError('Destination already exists; migration never overwrites data')
    staging = destination.with_name(destination.name+'.migrating')
    if staging.exists():
        raise RuntimeError('Previous staging directory exists; inspect it before retrying')
    staging.mkdir(parents=True)
    target = staging/'bookhub.sqlite3'
    report = {'started_at':int(time.time()),'tables':{},'files':{}}
    with readonly(source/'myshelf/library.db') as old, sqlite3.connect(target) as db:
        old.backup(db)
        for row in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            table=row['name']
            report['tables'][table]=old.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    oldroot = source/'reader-sync/data'
    with readonly(oldroot/'sync.sqlite3') as old, sqlite3.connect(target) as db:
        metadata = old.execute("SELECT value FROM metadata WHERE key='data_root'").fetchone()
        original_root = Path(metadata[0]) if metadata else Path('/data')
        for row in old.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            name, sql = row['name'], row['sql']
            newname = 'kindle_books' if name=='books' else name
            sql = re.sub(r'\bbooks\b','kindle_books',sql)
            db.execute(sql)
            columns = [r['name'] for r in old.execute(f'PRAGMA table_info("{name}")')]
            rows = old.execute(f'SELECT * FROM "{name}"').fetchall()
            cols=','.join('"'+c+'"' for c in columns)
            db.executemany(f'INSERT INTO "{newname}" ({cols}) VALUES({",".join("?" for _ in columns)})', [tuple(r) for r in rows])
            report['tables'][newname]=len(rows)
        def moved(value):
            if not value:return value
            # Store the final path even while staging, then verify after activation.
            try:relative=Path(value).relative_to(original_root)
            except ValueError:relative=Path(value).relative_to(oldroot)
            return str(destination/relative)
        db.row_factory=sqlite3.Row
        for row in db.execute('SELECT key,kfx_path,epub_path,map_path,compact_path FROM kindle_books').fetchall():
            paths=[moved(row[k]) for k in ('kfx_path','epub_path','map_path','compact_path')]
            db.execute('UPDATE kindle_books SET kfx_path=?,epub_path=?,map_path=?,compact_path=? WHERE key=?',(*paths,row['key']))
        for row in db.execute('SELECT id,token,payload FROM jobs').fetchall():
            payload=json.loads(row['payload']);payload['path']=moved(payload['path'])
            db.execute('UPDATE jobs SET token=?,payload=? WHERE id=?',(moved(row['token']),json.dumps(payload),row['id']))
        db.execute("INSERT INTO metadata VALUES('data_root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(destination),))
        # Old automatic jobs are not consent to new AI calls or device uploads.
        db.execute("UPDATE books SET summary_status=CASE WHEN trim(COALESCE(summary,''))='' THEN 'idle' ELSE 'ready' END,summary_job_id=NULL WHERE summary_status='pending'")
        db.execute("UPDATE recommendation_runs SET status='error',error='Прервано переходом на BookHub; повторный запуск — вручную' WHERE status='pending'")
        db.execute("UPDATE x3_uploads SET status='paused',error='Автоматическая отправка отключена; сохранено при миграции' WHERE status='queued'")
        for table, count in report['tables'].items():
            if db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != count:
                raise RuntimeError('Row count changed during migration: '+table)
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Destination integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Destination foreign keys are inconsistent')
    for src,relative in ((oldroot/'books','books'),(oldroot/'maps','maps'),
                         (source/'myshelf/static/covers','covers'),(source/'myshelf/incoming','incoming')):
        count=0
        if src.exists():
            shutil.copytree(src,staging/relative)
            for file in src.rglob('*'):
                if not file.is_file():continue
                copied=staging/relative/file.relative_to(src)
                def digest(p):
                    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
                if digest(file)!=digest(copied):raise RuntimeError('File copy verification failed')
                count+=1
        report['files'][relative]=count
    if (source/'myshelf/x3_device.json').exists():
        shutil.copy2(source/'myshelf/x3_device.json',staging/'x3_device.json')
    (staging/'migration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    staging.rename(destination)
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',type=Path,required=True)
    ap.add_argument('--destination',type=Path,required=True)
    args=ap.parse_args()
    print(json.dumps(migrate(args.source,args.destination),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
