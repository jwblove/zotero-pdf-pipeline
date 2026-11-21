import os
import stat

import pytest


@pytest.fixture
def assert_private():
    def check(path):
        if os.name != 'nt':
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
            return
        import win32security as security

        descriptor = security.GetNamedSecurityInfo(
            str(path), security.SE_FILE_OBJECT,
            security.DACL_SECURITY_INFORMATION | security.OWNER_SECURITY_INFORMATION)
        acl = descriptor.GetSecurityDescriptorDacl()
        assert acl is not None and acl.GetAceCount() == 1
        (ace_type, _), _, sid = acl.GetAce(0)
        assert ace_type == security.ACCESS_ALLOWED_ACE_TYPE
        import win32api
        import win32con
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            assert sid == security.GetTokenInformation(token, security.TokenUser)[0]
        finally:
            token.Close()
    return check
