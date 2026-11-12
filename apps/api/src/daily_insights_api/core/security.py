import base64
import hashlib
import hmac
import re
import secrets
from functools import lru_cache

PASSWORD_MIN_LENGTH = 8
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 5


class PasswordPolicyError(ValueError):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            f"password must contain at least {PASSWORD_MIN_LENGTH} characters"
        )


def validate_account_password(password: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9]{8,}", password) is None:
        raise PasswordPolicyError(
            "password must use only English letters and digits, with at least 8 characters"
        )


def hash_password(password: str, pepper: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        (password + pepper).encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(derived).decode(),
        )
    )


def verify_password(password: str, encoded: str, pepper: str) -> bool:
    try:
        algorithm, n, r, p, salt_encoded, expected_encoded = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_encoded)
        expected = base64.urlsafe_b64decode(expected_encoded)
        actual = hashlib.scrypt(
            (password + pepper).encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_needs_rehash(encoded: str) -> bool:
    try:
        algorithm, n, r, p, *_ = encoded.split("$")
        return (
            algorithm != "scrypt" or int(n) != SCRYPT_N or int(r) != SCRYPT_R or int(p) != SCRYPT_P
        )
    except (ValueError, TypeError):
        return True


@lru_cache(maxsize=4)
def dummy_password_hash(pepper: str) -> str:
    return hash_password("InvalidPassword123!", pepper)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
