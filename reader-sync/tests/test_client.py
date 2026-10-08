"""Real Lua client + real HTTP server + copies of user fixtures. LIPC is simulated."""
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import uvicorn
from app.main import create_app
from app.worker import run_next

ROOT=Path(__file__).resolve().parents[1]
KEY='XNZDUFSVLCHEUKLM374WPKP7762ZJ6J4'

@unittest.skipUnless(os.environ.get('SYNC_FIXTURES'),'Set SYNC_FIXTURES to test with local KFX/YJF files')
class ClientTests(unittest.TestCase):
    def test_upload_download_open_reader_offline_and_close_push(self):
        fixtures=Path(os.environ['SYNC_FIXTURES'])
        source=next(fixtures.glob('endi*.kfx'))
        sidecar=next(source.with_suffix('.sdr').glob('*.yjf'))
        untouched=sidecar.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            temp=Path(directory);home=temp/'client';books=temp/'books';bin=temp/'bin'
            shutil.copytree(ROOT.parent/'kindle-client',home,ignore=shutil.ignore_patterns('config.json','state','maps'))
            bin.mkdir();books.mkdir()
            dest=books/source.name;shutil.copyfile(source,dest)
            yjf=dest.with_suffix('.sdr')/sidecar.name;yjf.parent.mkdir();yjf.write_bytes(untouched)
            app=create_app(temp/'server','test-key','reader','password');store=app.state.store
            sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            self.addCleanup(lambda: (setattr(server,'should_exit',True),thread.join(5),sock.close()))
            for _ in range(100):
                if server.started:break
                time.sleep(.02)
            self.assertTrue(server.started)
            config=dict(server=f'http://127.0.0.1:{port}',api_key='test-key',device_id='kindle-test')
            (home/'config.json').write_text(json.dumps(config))
            (temp/'mode').write_text('library')
            def script(name,code):
                file=bin/name;file.write_text(code);file.chmod(0o755)
            script('sqlite3','#!'+sys.executable+"\nimport os,sys\nassert sys.argv[1]=='-readonly'\nassert not os.path.exists(os.environ['MOCK_ROOT']+'/deny-sqlite'), 'cc.db accessed on sleep path'\nprint(os.environ['MOCK_ROW'])\n")
            script('lipc-get-prop','#!'+sys.executable+"\nimport os,sys\nfrom pathlib import Path\np=Path(os.environ['MOCK_ROOT'])\nmode=(p/'mode').read_text()\nif sys.argv[-1]=='activeApp': print('com.lab126.booklet.reader' if mode=='reader' else 'com.lab126.KPPMainApp')\nelif sys.argv[-1]=='activeContext': print(':1:file://'+os.environ['MOCK_BOOK'] if mode=='reader' else ':0:app://com.lab126.KPPMainApp?view=KPP_LIBRARY')\n")
            script('lipc-set-prop','#!'+sys.executable+"\nimport os\nfrom pathlib import Path\np=Path(os.environ['MOCK_ROOT'])\nassert 'KPP_LIBRARY' in __import__('sys').argv[-1]\nif (p/'flush.yjf').exists():\n Path(os.environ['MOCK_YJF']).write_bytes((p/'flush.yjf').read_bytes())\n (p/'flush.yjf').unlink()\n(p/'mode').write_text('library')\n")
            script('lipc-wait-event','#!'+sys.executable+"\nimport os\nwith open(os.environ['MOCK_ROOT']+'/control.fifo') as f:\n for line in f: print(line.strip(),flush=True)\n")
            # Kindle BusyBox: sleep accepts integers only, usleep takes microseconds.
            script('sleep','#!/bin/sh\ncase "$1" in *[!0-9]*|"") echo "sleep: invalid number \'$1\'" >&2;exit 1;; esac\nexec /bin/sleep "$1"\n')
            script('usleep','#!'+sys.executable+"\nimport sys,time\ntime.sleep(int(sys.argv[1])/1000000)\n")
            script('lua-host','#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(ROOT/'tests/lua_runner.py'))+' "$@"\n')
            env=dict(os.environ,KINDLE_SYNC_HOME=str(home),KINDLE_SYNC_LUA=str(bin/'lua-host'),KINDLE_POS_CC_DB=str(temp/'cc.db'),
                     MOCK_ROOT=str(temp),MOCK_BOOK=str(dest),MOCK_YJF=str(yjf),
                     MOCK_ROW='\x1f'.join([KEY,'Test book',str(dest),'0.14','123']),PATH=str(bin)+':'+os.environ['PATH'])
            def run(*args,success=True):
                result=subprocess.run([str(home/'bin/kindle-agent'),*args],env=env,capture_output=True,text=True,timeout=60)
                if success:self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                else:self.assertNotEqual(0,result.returncode)
                return result
            def native(pid):
                result=subprocess.run([str(bin/'lua-host'),str(home/'bin/kindle-pos'),'set-pid',KEY,str(pid),str(home/'maps'/f'{KEY}.posmap')],env=env,capture_output=True,text=True)
                self.assertEqual(0,result.returncode,result.stderr)
            def current():
                result=subprocess.run([str(bin/'lua-host'),str(home/'bin/kindle-pos'),'export',KEY],env=env,capture_output=True,text=True)
                self.assertEqual(0,result.returncode,result.stderr)
                return int(result.stdout.split('\t')[3])
            run('doctor')
            run('scan')
            self.assertEqual('queued',store.book(KEY)['map_status'])
            self.assertTrue(run_next(store));self.assertEqual('ready',store.book(KEY)['map_status'])
            run('sync')
            self.assertTrue((home/'maps'/f'{KEY}.posmap').exists())
            # The fixture YJF position (it differs between fixture sets) is published as is.
            initial=current();self.assertLess(initial,228605)
            self.assertEqual(initial,store.book(KEY)['pid'])
            # Higher server position closes the reader and is applied/read back.
            store.update_position(KEY,228605,'x3')
            (temp/'mode').write_text('reader');run('observe');run('sync')
            self.assertEqual('library',(temp/'mode').read_text());self.assertEqual(228605,current())
            self.assertEqual(228605,store.library()['applied'][0]['observed_pid'])
            # Native memory ahead of both the stale YJF and server wins after close.
            native(455348);(temp/'flush.yjf').write_bytes(yjf.read_bytes());native(initial)
            (temp/'mode').write_text('reader');run('sync')
            self.assertEqual(455348,current());self.assertEqual(455348,store.book(KEY)['pid'])
            # Cover event captures without cc.db or HTTP. Offline upload keeps state.
            native(500000);(temp/'mode').write_text('reader');(temp/'deny-sqlite').touch()
            run('sleep');self.assertEqual('library',(temp/'mode').read_text())
            candidate=home/'state/agent/pending'/f'{KEY}.json'
            self.assertEqual(500000,json.loads(candidate.read_text())['pid'])
            config['server']='http://127.0.0.1:1';(home/'config.json').write_text(json.dumps(config))
            run('push',success=False);self.assertEqual(455348,store.book(KEY)['pid'])
            config['server']=f'http://127.0.0.1:{port}';(home/'config.json').write_text(json.dumps(config))
            # Still suspended, cc.db still forbidden: send cached point on close.
            run('push');self.assertEqual(500000,store.book(KEY)['pid'])
            (temp/'deny-sqlite').unlink();run('wake');run('sync')
            # A newer offline peak must survive a subsequent local backward jump.
            native(600000);run('capture',KEY)
            native(510000);run('capture',KEY)
            self.assertEqual(600000,json.loads(candidate.read_text())['pid'])
            run('sync');self.assertEqual(600000,store.book(KEY)['pid'])
            run('sync');self.assertEqual(600000,current())
            # Reset: the stale close push and the unchanged local point are held back,
            # the first real move (even backwards) becomes the new position.
            store.reset_position(KEY)
            run('push');self.assertIsNone(store.book(KEY)['pid'])
            run('sync');self.assertIsNone(store.book(KEY)['pid']);self.assertFalse(candidate.exists())
            run('capture',KEY);run('push');self.assertIsNone(store.book(KEY)['pid'])
            native(1000);run('capture',KEY);self.assertEqual(1000,json.loads(candidate.read_text())['pid'])
            run('push');self.assertEqual(1000,store.book(KEY)['pid'])
            run('sync');self.assertEqual(1000,store.book(KEY)['pid'])
            # Exercise the real daemon event routing and orderly stop, with fake LIPC.
            os.mkfifo(temp/'control.fifo')
            native(650000);(temp/'mode').write_text('reader')
            run('start')
            try:
                deadline=time.monotonic()+8
                control=None
                while time.monotonic()<deadline:
                    try:
                        control=os.open(temp/'control.fifo',os.O_WRONLY|os.O_NONBLOCK);break
                    except OSError:time.sleep(.1)
                self.assertIsNotNone(control,'LIPC listener did not start')
                os.write(control,b'goingToScreenSaver\n')
                deadline=time.monotonic()+12
                while time.monotonic()<deadline and store.book(KEY)['pid']!=650000:time.sleep(.1)
                self.assertEqual(650000,store.book(KEY)['pid'])
                self.assertEqual('library',(temp/'mode').read_text())
                os.write(control,b'outOfScreenSaver\n')
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and (home/'state/agent/suspended').exists():time.sleep(.1)
                self.assertFalse((home/'state/agent/suspended').exists())
            finally:
                run('stop')
                if control is not None:os.close(control)
            self.assertIn('stopped',run('status').stdout)
            self.assertFalse(list(home.rglob('*.bak')))
            self.assertEqual([yjf],list(books.rglob('*.yjf')))
        self.assertEqual(untouched,sidecar.read_bytes())

if __name__=='__main__':unittest.main()


KEY_A='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1'
KEY_B='BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2'


class PositionAnswerTests(unittest.TestCase):
    """The shipped client reads a position answer with its own JSON library; a field it does not know changes nothing."""

    def test_client_reads_the_fields_it_uses_next_to_start_pid(self):
        from fastapi.testclient import TestClient
        temp=Path(self.enterContext(tempfile.TemporaryDirectory()))
        app=create_app(temp/'server','test-key','reader','password');store=app.state.store
        client=self.enterContext(TestClient(app));auth={'Authorization':'Bearer test-key'}
        store.inventory('kindle-test',[dict(key=KEY_A,fingerprint='a'*64)])
        script=temp/'read.lua'
        script.write_text("local r=dofile(arg[1]).decode(io.read('*a'))\n"
                          "print(tostring(r.pid),tostring(r.epoch),tostring(r.asset_version),tostring(r.revision),tostring(r.start_pid))\n")
        def read(answer):
            result=subprocess.run([sys.executable,str(ROOT/'tests/lua_runner.py'),str(script),str(ROOT.parent/'kindle-client/lib/json.lua')],
                                  input=answer,capture_output=True,text=True,timeout=30)
            self.assertEqual(0,result.returncode,result.stderr)
            return result.stdout.split()
        route=f'/api/books/{KEY_A}/position'
        self.assertEqual(['nil','0','0','0','nil'],read(client.get(route,headers=auth).text))  # no map: start_pid is null
        from app.engine.kindle_map import PositionMap,write_map
        text='A cover line comes first, then the text of the book itself.'
        mapping=PositionMap(dict(version=1,book=dict(key=KEY_A),total_positions=1+len(text),
                                 chunks=[dict(pid=0,eid=1,offset=0,length=1,text=None),dict(pid=1,eid=2,offset=0,length=len(text),text=text)]))
        write_map(mapping,temp/'map.json',temp/'map.posmap')
        with store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_path=?,compact_path=?,map_status='ready',asset_version=1 WHERE key=?",(str(temp/'map.json'),str(temp/'map.posmap'),KEY_A))
        store.update_position(KEY_A,20,'kindle',1)
        self.assertEqual(['20','0','1','1','1'],read(client.get(route,headers=auth).text))
        self.assertEqual(['nil','1','1','2','1'],read(client.post(f'/api/books/{KEY_A}/reset',headers=auth).text))


class InventoryTests(unittest.TestCase):
    """No real books: the Kindle catalogue changes between syncs and the server hears on the next one."""

    def setUp(self):
        temp=Path(self.enterContext(tempfile.TemporaryDirectory()));self.temp=temp
        self.home=temp/'client';self.books=temp/'books';self.bin=temp/'bin'
        shutil.copytree(ROOT.parent/'kindle-client',self.home,ignore=shutil.ignore_patterns('config.json','state','maps'))
        self.books.mkdir();self.bin.mkdir()
        app=create_app(temp/'server','test-key','reader','password');self.store=app.state.store
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        self.addCleanup(lambda: (setattr(server,'should_exit',True),thread.join(5),sock.close()))
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        (self.home/'config.json').write_text(json.dumps(dict(server=f'http://127.0.0.1:{port}',api_key='test-key',device_id='kindle-test')))
        def script(name,code):
            file=self.bin/name;file.write_text(code);file.chmod(0o755)
        script('sqlite3','#!'+sys.executable+"\nimport os,sys\nassert sys.argv[1]=='-readonly'\nprint(open(os.environ['MOCK_ROWS']).read(),end='')\n")
        # Every hashed book file is logged: unchanged books must not be hashed again.
        real=shutil.which('sha256sum')
        script('sha256sum','#!/bin/sh\ncase "$1" in *.kfx) echo "$1" >> "$MOCK_ROOT/hashed";; esac\nexec '+real+' "$@"\n')
        script('lua-host','#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(ROOT/'tests/lua_runner.py'))+' "$@"\n')
        self.env=dict(os.environ,KINDLE_SYNC_HOME=str(self.home),KINDLE_SYNC_LUA=str(self.bin/'lua-host'),
                      KINDLE_POS_CC_DB=str(temp/'cc.db'),MOCK_ROOT=str(temp),MOCK_ROWS=str(temp/'rows'),
                      PATH=str(self.bin)+':'+os.environ['PATH'])
        self.catalogue={}

    def put(self,key,data):
        path=self.books/f'book_{key}.kfx';path.write_bytes(data);self.catalogue[key]=path;self.write_rows()

    def drop(self,key):
        self.catalogue.pop(key).unlink();self.write_rows()

    def write_rows(self):
        (self.temp/'rows').write_text(''.join('\x1f'.join([k,'Title '+k[-1],str(p),'0','1'])+'\n' for k,p in self.catalogue.items()))

    def sync(self):
        (self.temp/'hashed').unlink(missing_ok=True)
        result=subprocess.run([str(self.home/'bin/kindle-agent'),'sync'],env=self.env,capture_output=True,text=True,timeout=60)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        hashed=self.temp/'hashed'
        # A book is hashed on scan and again right before upload: count books, not calls.
        return sorted({Path(line).name for line in hashed.read_text().split()}) if hashed.exists() else []

    def present(self,key):
        with self.store.connect() as db:
            row=db.execute('SELECT present FROM kindle_books WHERE key=?',(key,)).fetchone()
        return row and row[0]

    def inventory(self):
        return {b['key']:b for b in json.loads((self.home/'state/agent/inventory.json').read_text())}

    def test_catalogue_changes_reach_server_on_next_sync(self):
        self.put(KEY_A,b'A'*1000)
        self.assertEqual(self.sync(),[f'book_{KEY_A}.kfx'])  # first sync: full scan
        self.assertEqual(self.present(KEY_A),1)

        self.put(KEY_B,b'B'*1000)
        self.assertEqual(self.sync(),[f'book_{KEY_B}.kfx'])  # only the new book is hashed
        self.assertEqual(self.present(KEY_B),1)

        self.assertEqual(self.sync(),[])  # nothing changed: no hashing at all

        old=self.inventory()[KEY_B]['fingerprint']
        self.put(KEY_B,b'B'*2000)
        self.assertEqual(self.sync(),[f'book_{KEY_B}.kfx'])
        self.assertNotEqual(self.inventory()[KEY_B]['fingerprint'],old)

        self.drop(KEY_A)
        self.assertEqual(self.sync(),[])
        self.assertEqual(self.present(KEY_A),0);self.assertEqual(list(self.inventory()),[KEY_B])

        (self.home/'state/agent/last-scan').write_text(str(int(time.time())-601))
        self.assertEqual(self.sync(),[f'book_{KEY_B}.kfx'])  # periodic full scan still hashes everything

    def test_book_changed_during_upload_forces_full_scan(self):
        self.put(KEY_A,b'A'*1000)
        self.sync()
        # Same size, different bytes: the cheap check misses it, the upload check must not.
        self.put(KEY_A,b'Z'*1000)
        with self.store.connect(write=True) as db:
            db.execute('UPDATE kindle_books SET kfx_path=NULL WHERE key=?',(KEY_A,))
        self.sync()
        self.assertFalse((self.home/'state/agent/last-scan').exists())
        self.assertEqual(self.sync(),[f'book_{KEY_A}.kfx'])
        self.assertTrue((self.home/'state/agent/last-scan').exists())
