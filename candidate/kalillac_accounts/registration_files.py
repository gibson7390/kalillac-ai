"""Fail-closed Linux descriptor loader shared by all registration protected files."""
from __future__ import annotations
import os
import stat
import sys
from .registration_crypto import RegistrationUnavailable

def _identity(s):
    return (s.st_dev,s.st_ino,s.st_uid,s.st_gid,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

def _acl(fd):
    # Missing enforcement support is a refusal, never a permissive fallback.
    names=os.listxattr(fd)
    if any(n in names for n in ("system.posix_acl_access","system.posix_acl_default")):
        raise RegistrationUnavailable()

def _directory(fd,gid):
    s=os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_gid not in (0,gid) or stat.S_IMODE(s.st_mode)&0o7022:
        raise RegistrationUnavailable()
    _acl(fd)
    return _identity(s)

def read_protected(path,ceiling):
    fds=[]
    try:
        if sys.platform!="linux" or os.geteuid()==0 or os.getegid()==0:
            raise RegistrationUnavailable()
        if not isinstance(path,str) or not path.startswith("/") or "\0" in path or any(c in ("",".","..") for c in path.split("/")[1:]):
            raise RegistrationUnavailable()
        gid=os.getegid()
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
        fd=os.open("/",flags);fds.append(fd)
        dirs=[(fd,_directory(fd,gid))]
        parts=path.split("/")[1:]
        for component in parts[:-1]:
            fd=os.open(component,flags,dir_fd=fd);fds.append(fd)
            dirs.append((fd,_directory(fd,gid)))
        file_fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=fd);fds.append(file_fd)
        s=os.fstat(file_fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_gid!=gid or stat.S_IMODE(s.st_mode)!=0o440 or s.st_nlink!=1 or not 0<s.st_size<=ceiling:
            raise RegistrationUnavailable()
        _acl(file_fd);before=_identity(s)
        raw=bytearray()
        while len(raw)<=ceiling:
            chunk=os.read(file_fd,min(4096,ceiling+1-len(raw)))
            if not chunk:break
            raw.extend(chunk)
        if len(raw)>ceiling or len(raw)!=s.st_size or _identity(os.fstat(file_fd))!=before:
            raise RegistrationUnavailable()
        _acl(file_fd)
        for ancestor,identity in dirs:
            if _directory(ancestor,gid)!=identity:raise RegistrationUnavailable()
        return bytes(raw)
    except Exception:
        raise RegistrationUnavailable() from None
    finally:
        for fd in reversed(fds):os.close(fd)
