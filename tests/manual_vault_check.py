"""Opt-in OS check with a unique synthetic account; never reads real accounts."""
from secrets import token_urlsafe
from uuid import uuid4
from rectifier.credentials import read_password, save_password_new, forget_password


def main():
    account = "rectifier_test_" + uuid4().hex
    password = token_urlsafe(24)
    if read_password(account) is not None:
        raise RuntimeError("synthetic namespace collision")
    try:
        save_password_new(account, password)
        if read_password(account) != password:
            raise RuntimeError("synthetic credential mismatch")
        print("Windows vault save/read: PASS")
    finally:
        forget_password(account)
        password = None
    if read_password(account) is not None:
        raise RuntimeError("synthetic credential cleanup failed")
    print("Windows vault cleanup: PASS")


if __name__ == "__main__":
    main()
