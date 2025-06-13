from __future__ import annotations
from hashlib import sha3_512
from io import BytesIO
from typing import Optional, Self
from base64 import urlsafe_b64encode, urlsafe_b64decode
from dataclasses import dataclass, field
from hmac import compare_digest
from enum import Flag, auto, Enum
from datetime import datetime
from urllib.parse import urlparse
import os
import json
import base64
import secrets
import uuid
import sys
import webauthn
from cachetools import LRUCache, cached, TTLCache
from flask import Response, Request
from webauthn.helpers.structs import PublicKeyCredentialCreationOptions, RegistrationCredential, PublicKeyCredentialRequestOptions, AuthenticationCredential
from webauthn.helpers.exceptions import InvalidRegistrationResponse
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.hashes import SHA3_512
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from . import database as _database
from . import consts
from .exceptions import (
    NotFoundError,
    AlreadyExistsError,
    PasswordTooLong,
    PasswordTooShort,
    UsernameTooLong,
    UsernameTooShort,
    UsernameInvalidCharacters,
    InvalidCredentials,
    NoSession,
    CannotBeNamedAnonymous,
    NeedsNotOldLogin,
    NeedsOldLogin
)

AUTH_SALT = str(os.getenv("AUTH_SALT"))

class LoginType(Enum):
    WEAK = 0
    SHA3_512_PBKDF2HMAC_100000 = 1

@dataclass(frozen=True)
class LoginData:
    data: str
    login_token: str
    login_type: LoginType = field(default=LoginType.WEAK)

@dataclass(frozen=True)
class SessionData:
    data: str
    @classmethod
    def from_request(cls, request: Request) -> Optional[Self]:
        if SESSION_DATA_COOKIE_NAME not in request.cookies:
            return None
        return cls(request.cookies[SESSION_DATA_COOKIE_NAME])

@dataclass
class Session:
    session_data: SessionData
    creation_time: datetime
    username: str
    session_name: str
    settings: Settings
    permission_group: int
    @classmethod
    def create_empty_session(cls) -> Self:
        return cls(SessionData(""), datetime.now(), ANONYMOUS_USERNAME, ANONYMOUS_USERNAME, Settings.NONE, 1 - (1 << 31))
    
    @cached(TTLCache(256, 60))
    @staticmethod
    def from_session_data(database: _database.Database, session_data: SessionData) -> Session:
        session = database.get_session(session_data.data)
        if session is None:
            raise NoSession
        return session
    
    def is_empty(self) -> bool:
        return not self
    
    def __bool__(self) -> bool:
        return self.username != ANONYMOUS_USERNAME
    
    def get_user_profile(self, database: _database.Database) -> _database.UserProfile:
        user_profile = database.get_user_profile(self.username)
        if user_profile is None:
            raise NoSession()
        return user_profile

@dataclass(frozen=True)
class WebAuthnCredential:
    credential_public_key: bytes
    credential_id: bytes
    def to_string(self) -> str:
        data = {
            consts.FIELD_PUBLIC_KEY: self.credential_public_key,
            consts.FIELD_CRED_ID: self.credential_id
        }
        json_encoded = json.dumps(data).encode()
        return base64.b64encode(json_encoded).decode()
    
    @classmethod
    def from_string(cls, string: str) -> Self:
        json_decoded = base64.b64decode(string.encode()).decode()
        data = json.loads(json_decoded)
        return cls(credential_public_key=data[consts.FIELD_PUBLIC_KEY], credential_id=data[consts.FIELD_CRED_ID])
    
    def save_to_database(self, database: _database.Database, session: Session):
        database.create_authkey(self.to_string(), self.credential_id, session.username, session.session_name)
    
    @classmethod
    def get_from_id(cls, database: _database.Database, credential_id: bytes):
        data = database.find_credential_by_id(credential_id)
        if not data:
            raise NoSession()
        return cls.from_string(data)
    
    def get_user_profile(self, database: _database.Database) -> _database.UserProfile:
        user = database.get_user_profile_by_credential_id(self.credential_id)
        if not user:
            raise NoSession()
        return user
    
class Settings(Flag):
    NONE = 0
    VIEW_MEMBERS = auto()
    _VIEW_MEMBER_SETTINGS = auto()
    VIEW_MEMBER_SETTINGS = VIEW_MEMBERS | _VIEW_MEMBER_SETTINGS
    _EDIT_MEMBER_SETTINGS = auto()
    EDIT_MEMBER_SETTINGS = VIEW_MEMBER_SETTINGS | _EDIT_MEMBER_SETTINGS
    _CREATE_MEMBERS = auto()
    CREATE_MEMBERS = VIEW_MEMBERS | _CREATE_MEMBERS
    _DISABLE_MEMBERS = auto()
    DISABLE_MEMBERS = VIEW_MEMBERS | _DISABLE_MEMBERS
    _VIEW_INVITED_MEMBERS = auto()
    VIEW_INVITED_MEMBERS = VIEW_MEMBERS | _VIEW_INVITED_MEMBERS
    _UNINVITE_MEMBERS = auto()
    UNINVITE_MEMBERS = VIEW_INVITED_MEMBERS | _UNINVITE_MEMBERS
    _RETRIEVE_INVITATION = auto()
    RETRIEVE_INVITATION = VIEW_INVITED_MEMBERS | _RETRIEVE_INVITATION
    ADMIN = (1 << 20) - 1
    SYS_ADMIN = (1 << 31) - 1 # Has to be last
    def get_translated_name(self):
        return consts.SETTINGS_NAME_TRANSLATIONS.get(self.name) or "intern: " + self.name

encode_b64 = urlsafe_b64encode
decode_b64 = urlsafe_b64decode

SESSION_DATA_COOKIE_NAME = "session"
VALID_CHARACTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
ANONYMOUS_USERNAME = "anonymous"
USERNAME_MAX_LENGTH = 32
USERNAME_MIN_LENGTH = 3
PASSWORD_MAX_LENGTH = 1024
PASSWORD_MIN_LENGTH = 5

def validate_username_and_password(username: str, password: str) -> None:
    username_constraints(username)
    password_constraints(password)

def username_constraints(username: str) -> None:
    if username.lower() == ANONYMOUS_USERNAME.lower():
        raise CannotBeNamedAnonymous()
    if len(username) < USERNAME_MIN_LENGTH:
        raise UsernameTooShort("Username must be between {USERNAME_MIN_LENGTH} and {USERNAME_MAX_LENGTH} characters long.")
    if len(username) > USERNAME_MAX_LENGTH:
        raise UsernameTooLong(f"Username must be between {USERNAME_MIN_LENGTH} and {USERNAME_MAX_LENGTH} characters long.")
    for char in username:
        if char not in VALID_CHARACTERS:
            raise UsernameInvalidCharacters("Username must consist of characters a-z, A-Z, 0-9, _ and -.")

def password_constraints(password: str):
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordTooShort(f"Password must be between {PASSWORD_MIN_LENGTH} and {PASSWORD_MAX_LENGTH} characters long.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise PasswordTooLong(f"Password must be between {PASSWORD_MIN_LENGTH} and {PASSWORD_MAX_LENGTH} characters long.")

def superhash(data: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        SHA3_512(),
        64,
        salt,
        iterations=100000
    )
    return kdf.derive(data)

def weak_create_login_data(username: str, password: str, login_token: Optional[str] = None) -> LoginData:
    unhashed_data = BytesIO()
    unhashed_data.write(len(username).to_bytes(1))
    unhashed_data.write(username.encode("utf-8"))
    unhashed_data.write(len(password).to_bytes(2))
    unhashed_data.write(password.encode("utf-8"))
    login_token = login_token or str(uuid.uuid4())
    unhashed_data.write(login_token.encode("utf-8"))
    hashed_data = sha3_512(unhashed_data.getbuffer()).digest()
    return LoginData(encode_b64(hashed_data).decode("utf-8"), login_token, LoginType.WEAK)

def create_login_data(username: str, password: str, login_token: Optional[str] = None) -> LoginData:
    unhashed_data = BytesIO()
    unhashed_data.write(len(username).to_bytes(1))
    unhashed_data.write(username.encode("utf-8"))
    unhashed_data.write(len(password).to_bytes(2))
    unhashed_data.write(password.encode("utf-8"))
    login_token = login_token or str(uuid.uuid4())
    unhashed_data.write(login_token.encode("utf-8"))
    unhashed_data.write(len(AUTH_SALT).to_bytes(8))
    unhashed_data.write(AUTH_SALT.encode("utf-8"))
    hashed_data = sha3_512(unhashed_data.getbuffer()).digest()
    base64_hashed_data = encode_b64(hashed_data)
    superhashed = superhash(base64_hashed_data, login_token.encode("utf-8"))
    encoded_superhash = encode_b64(superhashed).decode("utf-8")
    return LoginData(encoded_superhash, login_token, LoginType.SHA3_512_PBKDF2HMAC_100000)

def lookup_user_login_data(database: _database.Database, username: str) -> LoginData:
    data = database.get_login_data_by_username(username)
    if data is None:
        raise NotFoundError()
    login_data, login_token, login_type_raw = data
    return LoginData(login_data, login_token, LoginType(login_type_raw))

def create_session_data() -> SessionData:
    return SessionData(secrets.token_urlsafe(256))

def lookup_user_by_session_data(database: _database.Database, session_data: str) -> str:
    user = database.get_username_by_session_data(session_data)
    if user is None:
        raise NotFoundError()
    return user

def make_user(database: _database.Database, username: str, password: str, session_name: str, user_slot: str) -> SessionData:
    if database.has_username(username, except_user_id=user_slot):
        raise AlreadyExistsError()
    login_data = create_login_data(username, password)
    database.create_user(username, login_data.data, login_data.login_token, login_data.login_type.value, user_slot)
    return make_session(database, username, session_name)

def make_session(database: _database.Database, username: str, session_name: str) -> SessionData:
    session_data = create_session_data()
    database.add_session(session_data.data, username, session_name)
    return session_data

def remove_unfilled_user(database: _database.Database, username: str) -> None:
    success = database.remove_unfilled_user(username)
    if not success:
        raise NotFoundError()

def set_permission_group(database: _database.Database, username: str, permission_group: int) -> None:
    success = database.set_permission_group(username, permission_group)
    if not success:
        raise NotFoundError()

def set_settings(database: _database.Database, username: str, settings: Settings) -> None:
    success = database.set_settings(username, settings.value)
    if not success:
        raise NotFoundError()

def disable_user(database: _database.Database, username: str) -> str:
    success = database.disable_user(username)
    if not success:
        raise NotFoundError()
    return success

@cached(cache=LRUCache(1<<16, sys.getsizeof))
def check_session(database: _database.Database, session_data: SessionData) -> str:
    user = lookup_user_by_session_data(database, session_data.data)
    return user

def migrate_user_login_data(database: _database.Database, username: str, password: str):
    corrected_username = database.get_correctly_cased_username(username)
    if not corrected_username:
        raise NotFoundError()
    generated_login_data = create_login_data(corrected_username, password)
    database.migrate_login_data(username, generated_login_data.data, generated_login_data.login_token, generated_login_data.login_type.value)

def old_login(database: _database.Database, username: str, password: str, session_name: str, extra_password: Optional[str] = None) -> SessionData:
    if not database.has_username(username):
        raise NotFoundError()
    user_login_data = lookup_user_login_data(database, username)
    if user_login_data.login_type != LoginType.WEAK:
        raise NeedsNotOldLogin()
    corrected_username = database.get_correctly_cased_username(username)
    if corrected_username is None:
        raise NotFoundError()
    generated_login_data = weak_create_login_data(corrected_username, password, user_login_data.login_token)
    success = compare_digest(user_login_data.data, generated_login_data.data)
    if not success:
        raise InvalidCredentials()
    migrate_user_login_data(database, username, extra_password or password)
    return make_session(database, corrected_username, session_name)

def login(database: _database.Database, username: str, password: str, session_name: str) -> SessionData:
    if not database.has_username(username):
        raise NotFoundError()
    user_login_data = lookup_user_login_data(database, username)
    if user_login_data.login_type != LoginType.SHA3_512_PBKDF2HMAC_100000:
        raise NeedsOldLogin()
    corrected_username = database.get_correctly_cased_username(username)
    if corrected_username is None:
        raise NotFoundError()
    generated_login_data = create_login_data(corrected_username, password, user_login_data.login_token)
    success = compare_digest(user_login_data.data, generated_login_data.data)
    if not success:
        raise InvalidCredentials()
    return make_session(database, corrected_username, session_name)

def sign_up(database: _database.Database, username: str, password: str, session_name: str, user_slot: str) -> SessionData:
    validate_username_and_password(username, password)
    return make_user(database, username, password, session_name, user_slot)

def create_user_slot(database: _database.Database, settings: Settings, permission_group: int, temp_name: str) -> str:
    if database.has_username(temp_name):
        raise AlreadyExistsError()
    numeric_settings = settings.value
    return database.create_user_slot(numeric_settings, permission_group, temp_name)

def logout(database: _database.Database, response: Response, request: Request) -> Response:
    session_data = SessionData.from_request(request)
    if session_data is None:
        return response
    database.delete_session(session_data.data)
    response.set_cookie(SESSION_DATA_COOKIE_NAME, "", expires=0)
    return response

def extract_session(database: _database.Database, request: Request) -> Session:
    session_data = SessionData.from_request(request)
    if session_data is None:
        raise NoSession()
    return Session.from_session_data(database, session_data)

def extract_session_or_empty(database: _database.Database, request: Request) -> Session:
    try:
        return extract_session(database, request)
    except NoSession:
        return Session.create_empty_session()

def add_csrf_token(response: Response) -> Response:
    response.set_cookie(consts.FIELD_CSRF_TOKEN, secrets.token_urlsafe(128), max_age=consts.COOKIE_AGE * 2)
    return response

def verify_csrf_token(req: Request) -> None:
    csrf_header = req.headers.get(consts.FIELD_CSRF_TOKEN_HEADER, "csrf")
    csrf_cookie = req.cookies.get(consts.FIELD_CSRF_TOKEN, "csrf")
    if not compare_digest(csrf_header, csrf_cookie):
        raise NoSession()
    if consts.FIELD_CSRF_TOKEN not in req.cookies:
        raise NoSession()
    return

def extract_hostname(request: Request):
    return str(urlparse(request.base_url).hostname)

def prepare_credential_creation(user: _database.UserProfile, request: Request) -> PublicKeyCredentialCreationOptions:
    return webauthn.generate_registration_options(
        rp_id=extract_hostname(request),
        rp_name="Inconspicuous",
        user_id=str(uuid.uuid4()).encode(),
        user_name=user.username,
    )

access_creation_credentials_cache: TTLCache[str, PublicKeyCredentialCreationOptions] = TTLCache(1024, 600)

def access_creation_credentials(user: _database.UserProfile, request: Request) -> PublicKeyCredentialCreationOptions:
    if user.username in access_creation_credentials_cache:
        data = access_creation_credentials_cache[user.username]
    else:
        data = prepare_credential_creation(user, request)
        access_creation_credentials_cache[user.username] = data
    return data

def verify_and_save_credential(database: _database.Database, user: _database.UserProfile, session: Session, request: Request, registration_credential: RegistrationCredential):
    expected_challenge = access_creation_credentials(user, request)
    try:
        auth_verification = webauthn.verify_registration_response(
            credential=registration_credential,
            expected_challenge=expected_challenge.challenge,
            expected_origin=f"https://{extract_hostname(request)}",
            expected_rp_id=extract_hostname(request),
        )
    except InvalidRegistrationResponse:
        raise NoSession()
    credential = WebAuthnCredential(
        credential_public_key=auth_verification.credential_public_key,
        credential_id=auth_verification.credential_id,
    )
    credential.save_to_database(database, session)

def prepare_login_creation(request: Request) -> PublicKeyCredentialRequestOptions:
    authentication_options = webauthn.generate_authentication_options(
        rp_id=extract_hostname(request)
    )
    return authentication_options

access_login_credentials_cache: TTLCache[str, PublicKeyCredentialRequestOptions] = TTLCache(1024, 600)

def access_login_credentials(request: Request) -> PublicKeyCredentialRequestOptions:
    csrf_token = request.cookies.get(consts.FIELD_CSRF_TOKEN)
    if not csrf_token:
        raise NoSession()
    if csrf_token in access_login_credentials_cache:
        data = access_login_credentials_cache[csrf_token]
    else:
        data = prepare_login_creation(request)
        access_login_credentials_cache[csrf_token] = data
    return data

def delete_current_login_credentials(request: Request) -> None:
    csrf_token = request.cookies.get(consts.FIELD_CSRF_TOKEN)
    if csrf_token and csrf_token in access_login_credentials_cache:
        access_login_credentials_cache.pop(csrf_token)

def login_by_credential(database: _database.Database, authentication_credential: AuthenticationCredential, session_name: str, request: Request) -> SessionData:
    expected_challenge = access_login_credentials(request)
    stored_credential = WebAuthnCredential.get_from_id(database, webauthn.base64url_to_bytes(authentication_credential.id))
    webauthn.verify_authentication_response(
        credential=authentication_credential,
        expected_challenge=expected_challenge.challenge,
        expected_origin=f"https://{extract_hostname(request)}",
        expected_rp_id=extract_hostname(request),
        credential_public_key=stored_credential.credential_public_key,
        credential_current_sign_count=0
    )
    delete_current_login_credentials(request)
    user = stored_credential.get_user_profile(database)
    return make_session(database, user.username, session_name)

def access_login_type(database: _database.Database, username: str) -> LoginType:
    login_data = lookup_user_login_data(database, username)
    return login_data.login_type

def rsa_key_from_data(data: bytes) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(
        data,
        password=None,
        backend=default_backend()
    )
    assert isinstance(key, RSAPrivateKey)
    return key

def decrypt_rsa(data: str, private_key: RSAPrivateKey) -> str:
    ciphertext = base64.b64decode(data)
    plaintext = private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return base64.b64decode(plaintext).decode()
