"""Native Windows file handles and ACLs for the pipeline's local files."""
import msvcrt
import os
from contextlib import contextmanager, ExitStack

import pywintypes
import win32api
import win32con
import win32file
import win32security


def _open(path, access, share, creation):
    flags = win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS
    try:
        handle = win32file.CreateFile(str(path), access, share, None, creation, flags, None)
    except pywintypes.error as exc:
        raise OSError(None, exc.strerror, str(path), exc.winerror) from None
    try:
        attrs = win32file.GetFileInformationByHandle(handle)[0]
        if attrs & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('reparse point refused')
    except BaseException:
        handle.Close()
        raise
    return handle


@contextmanager
def pinned_directory(root):
    # Hold the directory and its ancestors without delete-sharing while publishing.
    with ExitStack() as stack:
        for path in [*reversed(root.parents), root]:
            handle = _open(path, 0, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                           win32con.OPEN_EXISTING)
            stack.callback(handle.Close)
        yield


def read_fd(path):
    handle = _open(path, win32con.GENERIC_READ, win32con.FILE_SHARE_READ, win32con.OPEN_EXISTING)
    return msvcrt.open_osfhandle(handle.Detach(), os.O_RDONLY | os.O_BINARY)


def lock_fd(path):
    handle = _open(path, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, win32con.OPEN_ALWAYS)
    return msvcrt.open_osfhandle(handle.Detach(), os.O_RDWR | os.O_BINARY)


def private_path(path, *, directory=False):
    """Set and check an owner-only protected DACL before saving private data."""
    security = win32security
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()
    acl = security.ACL()
    flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE if directory else 0
    acl.AddAccessAllowedAceEx(security.ACL_REVISION, flags, win32file.FILE_ALL_ACCESS, sid)
    dacl_info = security.DACL_SECURITY_INFORMATION
    security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                                  dacl_info | security.PROTECTED_DACL_SECURITY_INFORMATION,
                                  None, None, acl, None)
    descriptor = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT, dacl_info)
    actual = descriptor.GetSecurityDescriptorDacl()
    if actual is None or actual.GetAceCount() != 1 or actual.GetAce(0) != acl.GetAce(0):
        raise PermissionError('private ACL could not be verified')
