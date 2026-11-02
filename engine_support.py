"""State ownership, bounded evidence reads, and credential filtering."""
import ctypes
import json
import os
import re
from pathlib import Path

def no_links(path):
    raw=Path(path).absolute()
    for item in [raw,*raw.parents]:
        if item.is_symlink() or item.is_junction(): raise ValueError('Symlinks/junctions are not permitted here')
    return raw.resolve()

class StateLock:
    def __init__(self,directory):
        self.directory=no_links(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.file=(self.directory/'service.lock').open('a+b')
        if self.file.tell()==0: self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close(); self.file=None
            raise RuntimeError('Another local-worker bridge owns this state directory') from exc
    def close(self):
        if self.file: self.file.close(); self.file=None

def redact(text):
    text=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)','[REDACTED PRIVATE KEY]',text,flags=re.S)
    text=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})','[REDACTED TOKEN]',text)
    text=re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}','Bearer [REDACTED]',text)
    text=re.sub(r'''(?im)(\b(?:api_key|apikey|password|secret|access_token)\s*[:=]\s*)(["'])([^\r\n"']+)\2''',r'\1"[REDACTED]"',text)
    return text

def artifact_text(batch_root,path,cursor=0,page_size=12000):
    if isinstance(cursor,bool) or not isinstance(cursor,int) or cursor<0: raise ValueError('Invalid artifact cursor')
    root=no_links(batch_root); path=no_links(path)
    if not path.is_relative_to(root) or not path.is_file(): raise ValueError('Artifact is outside its batch or missing')
    if path.stat().st_size>16*1024*1024: raise ValueError('Artifact exceeds 16 MiB read limit')
    text=path.read_text(encoding='utf-8-sig',errors='replace')
    if '\0' in text: raise ValueError('Binary artifacts are not exposed as text')
    text=redact(text)
    if cursor>len(text): raise ValueError('Artifact cursor exceeds content length')
    end=min(len(text),cursor+page_size)
    return dict(content=text[cursor:end],next_cursor=end if end<len(text) else None,total_chars=len(text),redacted=True)

def context_text(workspace,files):
    root=no_links(workspace); parts=[]; size=0
    for name in files:
        path=no_links(root/name)
        if not path.is_relative_to(root): raise ValueError('Context path escapes worktree')
        text=path.read_text(encoding='utf-8') if path.exists() else '[DELETED]'
        size+=len(text.encode('utf-8'))
        if size>1024*1024: raise ValueError('Context exceeds 1 MiB; split the task')
        parts.append(f'=== {name} ===\n{text}\n')
    return '\n'.join(parts)

def pid_alive(pid):
    if not pid: return False
    if os.name=='nt':
        from ctypes import wintypes as w
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]; kernel.OpenProcess.restype=w.HANDLE
        kernel.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]; kernel.GetExitCodeProcess.restype=w.BOOL
        kernel.CloseHandle.argtypes=[w.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle: return ctypes.get_last_error()==5
        try:
            code=w.DWORD(); return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(int(pid),0); return True
    except ProcessLookupError: return False
    except PermissionError: return True
