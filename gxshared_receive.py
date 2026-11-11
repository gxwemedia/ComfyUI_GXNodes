"""Handle-relative received assets. Never reopen a checked child by full path.

The configured root may be a symlink/Junction; its opened target is the authority
for this request. Child reparse points/symlinks are rejected. Windows uses NT
directory-relative opens; Linux requires O_TMPFILE and /proc for fd publication.
Other filesystems/platforms fail closed at temporary-file creation. Linux binds
directory identity, not pathname location: another actor may move the authorized
directory; replacement links are never followed, but the returned relative name
may no longer locate the object. Windows directory handles deny delete sharing.
This does not defend against process
injection, privileged handle theft, kernel access or malicious filesystem drivers.
"""
import os
import sys
import uuid
from pathlib import PurePosixPath


class ReceiveError(OSError):
    pass


def _component(name):
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(c in name for c in '/\\:\x00') or name[-1] in '. '):
        raise ReceiveError("共享资源路径组件无效")
    return name


class _Owned:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Windows:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        class Unicode(c.Structure):
            _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', c.c_void_p)]
        class Attributes(c.Structure):
            _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                        ('ObjectName', c.POINTER(Unicode)), ('Attributes', w.ULONG),
                        ('SecurityDescriptor', c.c_void_p), ('SecurityQualityOfService', c.c_void_p)]
        class StatusUnion(c.Union):
            _fields_ = [('Status', c.c_long), ('Pointer', c.c_void_p)]
        class Status(c.Structure):
            _fields_ = [('Result', StatusUnion), ('Information', c.c_size_t)]
        class Tag(c.Structure):
            _fields_ = [('FileAttributes', w.DWORD), ('ReparseTag', w.DWORD)]
        class Rename(c.Structure):
            _fields_ = [('ReplaceIfExists', c.c_ubyte), ('RootDirectory', w.HANDLE),
                        ('FileNameLength', w.DWORD), ('FileName', w.WCHAR * 1)]
        self.Unicode, self.Attributes, self.Status, self.Tag, self.Rename = Unicode, Attributes, Status, Tag, Rename
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.n = c.WinDLL('ntdll')
        self.k.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
        self.k.CreateFileW.restype = w.HANDLE
        self.k.CloseHandle.argtypes, self.k.CloseHandle.restype = [w.HANDLE], w.BOOL
        self.k.GetFileInformationByHandleEx.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
        self.k.GetFileInformationByHandleEx.restype = w.BOOL
        self.k.SetFileInformationByHandle.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
        self.k.SetFileInformationByHandle.restype = w.BOOL
        self.n.NtCreateFile.argtypes = [c.POINTER(w.HANDLE),w.DWORD,c.POINTER(Attributes),c.POINTER(Status),c.c_void_p,w.ULONG,w.ULONG,w.ULONG,w.ULONG,c.c_void_p,w.ULONG]
        self.n.NtCreateFile.restype = c.c_long
        self.n.NtSetInformationFile.argtypes = [w.HANDLE,c.POINTER(Status),c.c_void_p,w.ULONG,c.c_int]
        self.n.NtSetInformationFile.restype = c.c_long
        self.n.RtlNtStatusToDosError.argtypes, self.n.RtlNtStatusToDosError.restype = [c.c_long], w.ULONG

    def error(self):
        return self.c.WinError(self.c.get_last_error())

    def check_directory(self, handle, allow_reparse=False):
        tag = self.Tag()
        if not self.k.GetFileInformationByHandleEx(handle,9,self.c.byref(tag),self.c.sizeof(tag)):
            raise self.error()
        if not tag.FileAttributes & 0x10 or (not allow_reparse and tag.FileAttributes & 0x400):
            raise ReceiveError('共享资源内部目录不可为链接')

    def root(self, path):
        handle = self.k.CreateFileW(os.path.abspath(path),0x100081,3,None,3,0x02000000,None)
        if handle == self.c.c_void_p(-1).value:
            raise self.error()
        try:
            self.check_directory(handle, allow_reparse=True)
            return handle
        except BaseException:
            self.k.CloseHandle(handle)
            raise

    def child(self, parent, name, directory):
        _component(name)
        raw = name.encode('utf-16-le')
        if len(raw) > 65532:
            raise ReceiveError('共享资源名称过长')
        buffer = self.c.create_string_buffer(raw + b'\0\0')
        text = self.Unicode(len(raw),len(raw)+2,self.c.cast(buffer,self.c.c_void_p))
        attributes = self.Attributes(self.c.sizeof(self.Attributes),parent,self.c.pointer(text),0x40,None,None)
        result, status = self.w.HANDLE(), self.Status()
        code = self.n.NtCreateFile(self.c.byref(result),0x1000A1 if directory else 0xC0110000,
                                  self.c.byref(attributes),self.c.byref(status),None,0x10 if directory else 0x80,
                                  3 if directory else 0,3 if directory else 2,
                                  0x200020 | (1 if directory else 0x40),None,0)
        if code < 0:
            raise self.c.WinError(self.n.RtlNtStatusToDosError(code))
        try:
            if directory:
                self.check_directory(result)
            return result.value
        except BaseException:
            self.k.CloseHandle(result)
            raise

    def rename(self, handle, parent, name):
        raw = _component(name).encode('utf-16-le')
        # Native FILE_RENAME_INFORMATION, class 10, not Win32 FileRenameInfo (3).
        # Keep the destination handle authoritative; never convert to a path.
        # WDK requires at least sizeof(struct) plus the filename byte length.
        buffer = self.c.create_string_buffer(self.c.sizeof(self.Rename) + len(raw))
        info = self.Rename.from_buffer(buffer)
        info.ReplaceIfExists, info.RootDirectory, info.FileNameLength = 0, parent, len(raw)
        self.c.memmove(self.c.addressof(buffer)+self.Rename.FileName.offset,raw,len(raw))
        status = self.Status()
        code = self.n.NtSetInformationFile(handle,self.c.byref(status),buffer,len(buffer),10)
        if code < 0:
            error = self.c.WinError(self.n.RtlNtStatusToDosError(code))
            error.ntstatus = code & 0xffffffff
            raise error

    def delete(self, handle):
        flag = self.c.c_ubyte(1)
        if not self.k.SetFileInformationByHandle(handle,4,self.c.byref(flag),1):
            raise self.error()


class ReceiveDirectory(_Owned):
    def __init__(self, handles, native=None):
        self.handles, self.native = handles, native

    @property
    def handle(self):
        if not self.handles:
            raise ReceiveError('共享资源目录已关闭')
        return self.handles[-1]

    def directory(self, components):
        # Child owner holds its own duplicate of the root/parent handle.
        if self.native:
            # Open '.' relative to an existing handle without public path traversal.
            # DuplicateHandle preserves identity and sharing constraints.
            c, w, k = self.native.c, self.native.w, self.native.k
            k.GetCurrentProcess.restype = w.HANDLE
            k.DuplicateHandle.argtypes = [w.HANDLE,w.HANDLE,w.HANDLE,c.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
            k.DuplicateHandle.restype = w.BOOL
            process, duplicate = k.GetCurrentProcess(), w.HANDLE()
            if not k.DuplicateHandle(process,self.handle,process,c.byref(duplicate),0,False,2):
                raise self.native.error()
            opened = [duplicate.value]
        else:
            opened = [os.dup(self.handle)]
        result = ReceiveDirectory(opened,self.native)
        try:
            for name in components:
                _component(name)
                if self.native:
                    opened.append(self.native.child(result.handle,name,True))
                else:
                    try:
                        os.mkdir(name,0o700,dir_fd=result.handle)
                    except FileExistsError:
                        pass
                    opened.append(os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=result.handle))
            return result
        except BaseException:
            result.close()
            raise

    def create_temporary(self):
        if self.native:
            import msvcrt
            handle = self.native.child(self.handle,'.gx-receive-'+uuid.uuid4().hex+'.tmp',False)
            try:
                fd = msvcrt.open_osfhandle(handle,os.O_RDWR|os.O_BINARY)
            except BaseException:
                try: self.native.delete(handle)
                finally: self.native.k.CloseHandle(handle)
                raise
        else:
            if sys.platform != 'linux' or not hasattr(os,'O_TMPFILE') or not os.path.isdir('/proc/self/fd'):
                raise ReceiveError('文件系统缺少安全匿名文件发布能力')
            fd = os.open('.',os.O_RDWR|os.O_TMPFILE|os.O_CLOEXEC,0o600,dir_fd=self.handle)
        try:
            os.set_inheritable(fd,False)
            source = os.fdopen(fd,'w+b')
        except BaseException:
            try:
                if self.native:
                    self.native.delete(msvcrt.get_osfhandle(fd))
            finally:
                os.close(fd)
            raise
        return PendingAsset(self,source)

    def close(self):
        while self.handles:
            handle = self.handles.pop()
            if self.native:
                self.native.k.CloseHandle(handle)
            else:
                os.close(handle)


class PendingAsset(_Owned):
    def __init__(self, directory, source):
        self.directory, self.source, self.published = directory, source, False

    def write(self, data):
        return self.source.write(data)

    def publish_unique(self, filename):
        _component(filename)
        if self.published:
            raise ReceiveError('资源已经发布')
        self.source.flush()
        stem, suffix = PurePosixPath(filename).stem, PurePosixPath(filename).suffix
        for index in range(10000):
            name = filename if index == 0 else f'{stem}_{index}{suffix}'
            try:
                if self.directory.native:
                    import msvcrt
                    self.directory.native.rename(msvcrt.get_osfhandle(self.source.fileno()),self.directory.handle,name)
                else:
                    os.link(f'/proc/self/fd/{self.source.fileno()}',name,dst_dir_fd=self.directory.handle,follow_symlinks=True)
                self.published = True
                return name
            except FileExistsError:
                continue
        raise ReceiveError('同名资源过多')

    def close(self):
        if self.source.closed:
            return
        try:
            if self.directory.native and not self.published:
                import msvcrt
                self.directory.native.delete(msvcrt.get_osfhandle(self.source.fileno()))
        finally:
            self.source.close()


def receive_root(configured_root):
    if os.name == 'nt':
        native = _Windows()
        return ReceiveDirectory([native.root(configured_root)],native)
    if os.name == 'posix' and hasattr(os,'O_NOFOLLOW') and os.open in os.supports_dir_fd:
        return ReceiveDirectory([os.open(configured_root,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)])
    raise ReceiveError('平台缺少安全目录句柄能力')
