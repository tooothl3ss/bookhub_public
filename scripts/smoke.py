"""Run inside the built image: concurrent startup and shared state across real processes."""
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
import requests


def main():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        env=dict(os.environ,BOOKHUB_DATA=folder,BOOKHUB_DB=str(root/'bookhub.sqlite3'),
                 SYNC_USER='test',SYNC_PASSWORD='password',SYNC_API_KEY='test-key',AI_URL='',AI_MODEL='')
        commands=[['uvicorn','bookhub.application:create_web_app','--factory','--host','127.0.0.1','--port','18088'],
                  ['uvicorn','bookhub.application:create_sync_app','--factory','--host','127.0.0.1','--port','18089'],
                  ['bookhub.worker']]
        processes=[]
        with (root/'processes.log').open('w+') as log:
            try:
                for command in commands:
                    processes.append(subprocess.Popen([sys.executable,'-m',*command],env=env,stdout=log,stderr=log))
                web='http://127.0.0.1:18088';sync='http://127.0.0.1:18089'
                for _ in range(100):
                    if any(p.poll() is not None for p in processes):raise RuntimeError('A process exited at startup')
                    try:
                        if all(requests.get(url+'/health',timeout=1).status_code==200 for url in (web,sync)) and (root/'worker-heartbeat.json').exists():break
                    except requests.RequestException:pass
                    time.sleep(.2)
                else:raise RuntimeError('Concurrent startup timed out')
                buf=io.BytesIO()
                with zipfile.ZipFile(buf,'w') as z:
                    z.writestr('META-INF/container.xml','<container><rootfile full-path="book.opf"/></container>')
                    z.writestr('book.opf','<package><metadata><title>Smoke</title></metadata><manifest><item id="c" href="ch.xhtml"/></manifest><spine><itemref idref="c"/></spine></package>')
                    z.writestr('ch.xhtml','<html><body><p>A paragraph long enough to move the reading position between devices.</p></body></html>')
                response=requests.post(web+'/api/epubs?filename=smoke.epub',data=buf.getvalue(),timeout=10)
                response.raise_for_status();bid=response.json()['book_id']
                doc=hashlib.md5(b'smoke.epub').hexdigest()
                headers={'x-auth-user':'test','x-auth-key':hashlib.md5(b'password').hexdigest()}
                for url,offset,device in [(sync,10,'first'),(web,30,'second')]:
                    r=requests.put(url+'/syncs/progress',headers=headers,json=dict(document=doc,progress='/body/DocFragment[1]/body/p[1]/text()[1].'+str(offset),percentage=offset/100,device='KOReader',device_id=device),timeout=5)
                    r.raise_for_status();assert r.json()['accepted'],r.text
                card=requests.get(web+f'/api/books/{bid}',timeout=5).json()
                assert card['epubs'][0]['percentage']==.3
                assert len(card['devices'])==2
                status=requests.get(web+'/api/reader/status',timeout=5).json()
                assert status['ok'] and status['epubs']==1,status
                assert requests.get(web+'/api/library',timeout=5).status_code==401
                assert requests.get(web+'/',timeout=5).status_code==200
                # The readers' catalogue: behind the sync password on the sync port and through the shelf's.
                for url in (sync,web):
                    assert requests.get(url+'/opds',timeout=5,allow_redirects=False).status_code==401
                    r=requests.get(url+'/opds',auth=('test','password'),timeout=5,allow_redirects=False)
                    assert r.status_code==200 and '<feed' in r.text,r.text
                    r=requests.get(url+f"/opds/b/{bid}-{response.json()['sha256'][:12]}.epub",auth=('test','password'),timeout=5)
                    assert r.content==buf.getvalue() and int(r.headers['content-length'])==len(r.content),r.status_code
                subprocess.run([sys.executable,'-m','bookhub.check'],env=env,check=True,stdout=log,stderr=log)
                with sqlite3.connect(root/'bookhub.sqlite3') as db:
                    assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0
                    assert db.execute('SELECT COUNT(*) FROM x3_uploads').fetchone()[0]==0
                print('PASS built image: concurrent web/sync/worker, shared EPUB, two readers, auth, UI, heartbeat, OPDS on both ports; no AI or X3 tasks')
            except BaseException:
                log.flush();log.seek(0);print(log.read(),file=sys.stderr)
                raise
            finally:
                for p in processes:p.terminate()
                for p in processes:
                    try:p.wait(timeout=10)
                    except subprocess.TimeoutExpired:p.kill();p.wait()


if __name__=='__main__':main()
