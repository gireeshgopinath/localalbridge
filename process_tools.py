"""Bounded subprocess execution. Windows jobs retain ownership of descendants."""
from __future__ import annotations
import contextlib
import ctypes
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

class WindowsJob:
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n,ctypes.c_ulonglong) for n in ['read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes']]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.k = ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]; self.k.CreateJobObjectW.restype=w.HANDLE
        self.k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]; self.k.SetInformationJobObject.restype=w.BOOL
        self.k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]; self.k.AssignProcessToJobObject.restype=w.BOOL
        self.k.CloseHandle.argtypes=[w.HANDLE]; self.k.CloseHandle.restype=w.BOOL
        self.handle=self.k.CreateJobObjectW(None,None)
        info=Extended(); info.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close(); raise ctypes.WinError(ctypes.get_last_error())
    def attach(self,proc):
        if self.handle and not self.k.AssignProcessToJobObject(self.handle,int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle); self.handle=None

def kill_tree(proc):
    if os.name=='nt':
        if proc.poll() is None:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    else:
        with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid,signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired): proc.wait(timeout=3)

def run_process(argv,cwd,out_dir,cancel,timeout,on_line=None,on_process=None,env=None):
    if timeout<=0: raise ValueError('timeout must be positive')
    out_dir=Path(out_dir); out_dir.mkdir(parents=True,exist_ok=True)
    stdout=out_dir/'stdout.log'; stderr=out_dir/'stderr.log'
    result=dict(code=None,error=None,elapsed_seconds=0,stdout=str(stdout.resolve()),stderr=str(stderr.resolve()))
    start=time.monotonic(); deadline=start+timeout; proc=None; reader=None; job=None; lines=queue.Queue(); eof=object()
    def read(pipe):
        try:
            for line in iter(pipe.readline,''): lines.put(line)
        except (OSError,ValueError): pass
        finally: lines.put(eof)
    try:
        with stdout.open('w',encoding='utf-8') as out, stderr.open('w',encoding='utf-8') as err:
            if cancel.is_set():
                result['error']='cancelled before spawn'; return result
            job=WindowsJob()
            proc=subprocess.Popen(list(argv),cwd=str(cwd),env=env,shell=False,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=err,encoding='utf-8',errors='replace',start_new_session=os.name!='nt',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            job.attach(proc)
            if on_process: on_process(proc.pid)
            reader=threading.Thread(target=read,args=(proc.stdout,),daemon=True); reader.start()
            try:
                while True:
                    if cancel.is_set(): raise RuntimeError('cancelled')
                    if time.monotonic()>=deadline: raise TimeoutError(f'timed out after {timeout:g}s')
                    if out.tell()>16*1024*1024 or stderr.stat().st_size>16*1024*1024: raise RuntimeError('log limit exceeded (16 MiB)')
                    try: line=lines.get(timeout=0.05)
                    except queue.Empty: continue
                    if line is eof:
                        proc.wait(timeout=max(0.01,deadline-time.monotonic())); break
                    out.write(line); out.flush()
                    if on_line: on_line(line)
            except Exception as exc:
                result['error']=str(exc) or type(exc).__name__
            finally:
                # Closing a job kills descendants even if their parent already exited.
                job.close()
                if proc.poll() is None: kill_tree(proc)
                if reader: reader.join(timeout=2)
                while not lines.empty():
                    line=lines.get_nowait()
                    if line is not eof: out.write(line)
                out.flush()
    except Exception as exc:
        result['error']=str(exc) or type(exc).__name__
    finally:
        if job: job.close()
        if proc:
            if proc.poll() is None: kill_tree(proc)
            if proc.stdout: proc.stdout.close()
            result['code']=proc.poll()
        if on_process:
            with contextlib.suppress(Exception): on_process(None)
        result['elapsed_seconds']=round(time.monotonic()-start,3)
    return result
