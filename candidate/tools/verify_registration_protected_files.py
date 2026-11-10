"""Disposable Linux root fixture driver for the production descriptor loader.

Run with sudo in isolated CI/WSL only. Creates one uniquely named /run fixture,
drops each reader to nobody, then removes exactly that fixture. No service or
deployment configuration is touched. All data is fixed synthetic JSON.
"""
from __future__ import annotations
import json,os,pathlib,shutil,stat,struct,sys,uuid
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from kalillac_accounts.registration_files import read_protected
from kalillac_accounts.registration_crypto import RegistrationUnavailable

def main():
    assert sys.platform=="linux" and os.geteuid()==0,"isolated root fixture required"
    base=pathlib.Path("/run")/("kalillac-registration-test-"+uuid.uuid4().hex)
    assert base.parent==pathlib.Path("/run") and not base.exists()
    base.mkdir(mode=0o750);os.chown(base,0,65534)
    results=[]
    def make(name,mode=0o440,data=b'{"synthetic":true}'):
        p=base/name;p.write_bytes(data);os.chown(p,0,65534);os.chmod(p,mode);return p
    def child_read(label,path,expected,ceiling=8192,*,drop=True,mutate=None):
        request_r,request_w=os.pipe();reply_r,reply_w=os.pipe()
        pid=os.fork()
        if pid==0:
            os.close(request_r);os.close(reply_w)
            try:
                if drop:os.setgroups([]);os.setgid(65534);os.setuid(65534)
                if mutate:
                    import kalillac_accounts.registration_files as loader
                    original=loader.os.read;seen=[False]
                    def read(fd,n):
                        data=original(fd,n)
                        if data and not seen[0]:
                            seen[0]=True;os.write(request_w,b"x");original(reply_r,1)
                        return data
                    loader.os.read=read
                try:
                    raw=read_protected(str(path),ceiling)
                    good=expected and raw==b'{"synthetic":true}'
                except RegistrationUnavailable:good=not expected
                os._exit(0 if good else 1)
            except BaseException:os._exit(2)
        os.close(request_w);os.close(reply_r)
        if mutate:
            assert os.read(request_r,1)==b"x"
            mutate();os.write(reply_w,b"x")
        os.close(request_r);os.close(reply_w)
        _,status=os.waitpid(pid,0)
        results.append({"case":label,"passed":os.waitstatus_to_exitcode(status)==0})
    try:
        valid=make("valid")
        child_read("valid descriptor with exact ownership",valid,True)
        child_read("root runtime rejected",valid,False,drop=False)
        for mode in [0o600,0o640,0o444,0o1440,0o4640]:
            p=make("mode-"+str(mode),mode);child_read("reject permission "+str(mode),p,False)
        p=make("wrong-owner");os.chown(p,65534,65534);child_read("reject owner",p,False)
        p=make("wrong-group");os.chown(p,0,1000);child_read("reject group",p,False)
        p=make("empty",data=b"");child_read("reject empty",p,False)
        p=make("oversize",data=b"x"*8193);child_read("reject credential ceiling",p,False)
        p=base/"symlink";p.symlink_to(valid);child_read("reject final symlink",p,False)
        p=base/"hardlink";os.link(valid,p);child_read("reject multiple links",p,False);p.unlink()
        p=base/"fifo";os.mkfifo(p,0o440);os.chown(p,0,65534);child_read("reject FIFO without blocking",p,False)
        child_read("reject dot component",str(base)+"/./valid",False)
        child_read("reject dotdot component",str(base)+"/../"+base.name+"/valid",False)
        child_read("reject relative path","relative",False)
        unsafe=base/"unsafe";unsafe.mkdir();os.chown(unsafe,0,65534);os.chmod(unsafe,0o770)
        p=unsafe/"payload";p.write_bytes(b'{"synthetic":true}');os.chown(p,0,65534);os.chmod(p,0o440)
        child_read("reject writable ancestor",p,False)
        link=base/"directory-link";link.symlink_to(unsafe,target_is_directory=True)
        child_read("reject ancestor symlink",link/"payload",False)
        # Real Linux POSIX access ACL, with mode still exactly 0440.
        acl=struct.pack("<I",2)+b"".join(struct.pack("<HHI",tag,perm,identity) for tag,perm,identity in [
            (1,4,0xffffffff),(2,4,1000),(4,4,0xffffffff),(16,4,0xffffffff),(32,0,0xffffffff)])
        p=make("acl-file");os.setxattr(p,"system.posix_acl_access",acl)
        child_read("reject extended file ACL",p,False)

        directory_acl=base/"acl-ancestor";directory_acl.mkdir(mode=0o750);os.chown(directory_acl,0,65534)
        payload=directory_acl/"payload";payload.write_bytes(b'{"synthetic":true}');os.chown(payload,0,65534);os.chmod(payload,0o440)
        ancestor_acl=struct.pack("<I",2)+b"".join(struct.pack("<HHI",tag,perm,identity) for tag,perm,identity in [
            (1,7,0xffffffff),(2,4,1000),(4,5,0xffffffff),(16,5,0xffffffff),(32,0,0xffffffff)])
        os.setxattr(directory_acl,"system.posix_acl_access",ancestor_acl)
        child_read("reject extended ancestor access ACL",payload,False)
        os.removexattr(directory_acl,"system.posix_acl_access");os.chmod(directory_acl,0o750)
        os.setxattr(directory_acl,"system.posix_acl_default",ancestor_acl)
        child_read("reject extended ancestor default ACL",payload,False)
        child_read("reject inode mutation",valid,False,mutate=lambda:valid.write_bytes(b'{"synthetic":false}'))
        valid.write_bytes(b'{"synthetic":true}');os.chmod(valid,0o440)
        child_read("reject ancestor mutation",valid,False,mutate=lambda:os.chmod(base,0o770))
        os.chmod(base,0o750)
        replacement=make("replacement")
        child_read("atomic replacement coherent or refuses",valid,False,mutate=lambda:os.replace(replacement,valid))
        child_read("replacement validated next startup",valid,True)
        print(json.dumps({"cases":results,"passed":sum(r["passed"] for r in results),"total":len(results)}))
        assert all(r["passed"] for r in results)
        print(f"RESULT: {len(results)}/{len(results)} protected-file checks passed")
    finally:
        assert base.resolve().parent==pathlib.Path("/run") and base.name.startswith("kalillac-registration-test-")
        shutil.rmtree(base)
if __name__=="__main__":main()
