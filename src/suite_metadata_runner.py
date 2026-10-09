"""Cancellable metadata batch using this entry file's private CLI worker."""
import copy
import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from suite_paths import APP_DIR,entry_command


def run_batch(records,library,config,stop,emit,worker_target=None):
    completed=0
    disabled=[]
    total=len(records)
    root=APP_DIR/'.tmp'
    root.mkdir(parents=True,exist_ok=True)
    emit('progress',{'completed':0,'total':total,'stage':'准备补全','current':''})
    for paper in records:
        if stop.is_set():break
        latest=copy.deepcopy(paper)
        request_path=None
        process=None
        channel=queue.Queue()
        final_received=False
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=root,suffix='.metadata.json',delete=False) as stream:
                request_path=Path(stream.name)
                json.dump(paper,stream,ensure_ascii=False)
            command=entry_command()+['_metadata-worker','--input',str(request_path),
                '--config',str(library.path.parent/'config.local.json'),
                '--sources',','.join(config.get('enabled_apis',['crossref','openalex','elsevier','wos'])),
                '--disabled',','.join(disabled)]
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,
                encoding='utf8',errors='replace',cwd=APP_DIR,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            def read_lines(stream=process.stdout,events=channel):
                try:
                    for line in stream:
                        try:events.put(json.loads(line))
                        except json.JSONDecodeError:pass
                except (OSError,ValueError):pass
                finally:events.put({'event':'eof'})
            reader=threading.Thread(target=read_lines,daemon=True)
            reader.start()
            deadline=time.monotonic()+max(120,10*int(config.get('timeout',25)))
            while not stop.is_set():
                try:event=channel.get(timeout=.1)
                except queue.Empty:
                    if time.monotonic()>deadline:
                        latest.setdefault('attempts',[]).append({'provider':'任务','result':'单篇补全超时'})
                        break
                    continue
                kind=event.get('event')
                if kind=='progress':
                    emit('progress',{'completed':completed,'total':total,'stage':event['message'],'current':paper.get('doi','')})
                elif kind in ('checkpoint','complete'):
                    latest=library.save(event['record'])
                    disabled=event.get('disabled',disabled)
                    emit('record',latest)
                    if kind=='complete':final_received=True;break
                elif kind in ('error','eof'):
                    latest.setdefault('attempts',[]).append({'provider':'任务','result':event.get('message','补全进程意外退出')})
                    break
        finally:
            if process is not None:
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
                if process.stdout:process.stdout.close()
            if request_path is not None:request_path.unlink(missing_ok=True)
        if stop.is_set() and not final_received:
            latest.setdefault('attempts',[]).append({'provider':'任务','result':'已停止；已取得的字段已保留'})
        elif final_received:completed+=1
        latest=library.save(latest)
        emit('record',latest)
        emit('progress',{'completed':completed,'total':total,'stage':'已停止' if stop.is_set() else '已保存','current':''})
    return f"{'已停止' if stop.is_set() else '补全结束'}：已处理 {completed}/{total} 篇"
