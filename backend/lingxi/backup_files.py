"""Offline archive extraction, invoked only by the privileged host runner."""
import os
from pathlib import Path
import shutil
import sys
import tarfile


def main():
    root=sys.argv[1]
    if root not in ('runtime','resources'):raise ValueError('Invalid restore scope')
    if sys.argv[2:] not in ([], ['--replace']):raise ValueError('Invalid extraction mode')
    replace=sys.argv[2:]==['--replace']
    target=Path('/app')/root
    if target.is_symlink():raise ValueError('Symlink restore root')
    target.mkdir(exist_ok=True)
    # Validate and spool before extracting. Never follow archived symlinks,
    # device nodes or paths escaping the selected mounted volume.
    import tempfile
    with tempfile.TemporaryFile() as stream:
        while chunk:=sys.stdin.buffer.read(1024**2):stream.write(chunk)
        stream.seek(0)
        with tarfile.open(fileobj=stream,mode='r:') as archive:
            members=archive.getmembers()
            for member in members:
                parts=member.name.rstrip('/').split('/')
                if parts[0]!=root or any(p in ('','..','.') for p in parts) or '\\' in member.name or not (member.isfile() or member.isdir()):raise ValueError('Unsafe archive member')
                path=Path('/app')/member.name
                if not path.resolve().is_relative_to(target.resolve()):raise ValueError('Unsafe destination')
                for ancestor in [path,*path.parents]:
                    if ancestor==target.parent:break
                    if ancestor.is_symlink():raise ValueError('Existing symlink in restore destination')
            if replace:
                # Only migration into an empty target / rollback uses this.
                # The complete archive was validated before touching files.
                for child in target.iterdir():
                    if child.is_symlink() or not child.is_dir():child.unlink()
                    else:shutil.rmtree(child)
            archive.extractall('/app',members=members,filter='data')
    # The application runs as uid 10001. Archive metadata cannot choose a user.
    for parent,dirs,files in os.walk(target,followlinks=False):
        os.chown(parent,10001,10001)
        for name in files:
            path=Path(parent)/name
            if not path.is_symlink():os.chown(path,10001,10001)


if __name__=='__main__':main()
