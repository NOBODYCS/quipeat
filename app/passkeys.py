"""WebAuthn/passkey scaffolding. Fully implemented but disabled until
PASSKEYS_ENABLED=true and the app is served over HTTPS (see README).
The routes are registered only when enabled, so password login is the
active path for now. The webauthn import is lazy so the app runs without
the optional dependency while passkeys are disabled."""
import base64
from datetime import datetime, timedelta, timezone

from . import db
from .config import settings

CHALLENGE_TTL_MIN = 5


def _wa():
    try:
        import webauthn
        from webauthn import (
            generate_authentication_options,
            generate_registration_options,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.cose import COSEAlgorithmIdentifier
        from webauthn.helpers.structs import (
            AuthenticatorAttachment,
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
        return {
            "webauthn": webauthn,
            "generate_authentication_options": generate_authentication_options,
            "generate_registration_options": generate_registration_options,
            "verify_authentication_response": verify_authentication_response,
            "verify_registration_response": verify_registration_response,
            "COSEAlgorithmIdentifier": COSEAlgorithmIdentifier,
            "AuthenticatorAttachment": AuthenticatorAttachment,
            "AuthenticatorSelectionCriteria": AuthenticatorSelectionCriteria,
            "PublicKeyCredentialDescriptor": PublicKeyCredentialDescriptor,
            "ResidentKeyRequirement": ResidentKeyRequirement,
            "UserVerificationRequirement": UserVerificationRequirement,
        }
    except ImportError:
        raise RuntimeError("pywebauthn is not installed; passkeys require `pip install pywebauthn`")


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def registration_options(user) -> dict:
    wa = _wa()
    creds = db.query("SELECT id FROM passkey_credentials WHERE user_id = ?", (user["id"],))
    options = wa["generate_registration_options"](
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_id=str(user["id"]).encode(),
        user_name=user["email"],
        user_display_name=user["display_name"] or user["email"],
        exclude_credentials=[wa["PublicKeyCredentialDescriptor"](id=b64d(c["id"])) for c in creds],
        authenticator_selection=wa["AuthenticatorSelectionCriteria"](
            authenticator_attachment=wa["AuthenticatorAttachment"].PLATFORM,
            resident_key=wa["ResidentKeyRequirement"].PREFERRED,
            user_verification=wa["UserVerificationRequirement"].PREFERRED,
        ),
        supported_pub_key_algs=[wa["COSEAlgorithmIdentifier"].ECDSA_SHA_256,
                                wa["COSEAlgorithmIdentifier"].RSASSA_PKCS1_v1_5_SHA_256],
    )
    challenge = b64e(options.challenge)
    _store_challenge(challenge, user["id"], "registration")
    return {
        "rp": {"id": settings.rp_id, "name": settings.rp_name},
        "user": {"id": b64e(str(user["id"]).encode()), "name": user["email"],
                 "displayName": user["display_name"] or user["email"]},
        "challenge": challenge,
        "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
        "timeout": 60000,
        "attestation": "none",
        "authenticatorSelection": {"residentKey": "preferred", "userVerification": "preferred"},
        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in creds],
    }


def complete_registration(user, credential: dict) -> None:
    wa = _wa()
    challenge = _take_challenge(credential.get("challenge", ""), user["id"], "registration")
    verification = wa["verify_registration_response"](
        credential=credential,
        expected_challenge=b64d(challenge),
        expected_origin=f"https://{settings.rp_id}",
        expected_rp_id=settings.rp_id,
    )
    db.execute(
        "INSERT INTO passkey_credentials(id, user_id, public_key, sign_count, transports)"
        " VALUES (?, ?, ?, ?, ?)",
        (b64e(verification.credential_id), user["id"],
         b64e(verification.credential_public_key), verification.sign_count,
         ",".join(credential.get("transports", []))),
    )


def authentication_options(email: str | None = None) -> dict:
    wa = _wa()
    creds = db.query("SELECT id FROM passkey_credentials") if not email else db.query(
        "SELECT pc.id FROM passkey_credentials pc JOIN users u ON u.id = pc.user_id WHERE u.email = ?",
        (email,))
    options = wa["generate_authentication_options"](
        rp_id=settings.rp_id,
        allow_credentials=[wa["PublicKeyCredentialDescriptor"](id=b64d(c["id"])) for c in creds],
        user_verification=wa["UserVerificationRequirement"].PREFERRED,
    )
    challenge = b64e(options.challenge)
    _store_challenge(challenge, None, "authentication")
    return {"challenge": challenge, "timeout": 60000, "rpId": settings.rp_id,
            "allowCredentials": [{"type": "public-key", "id": c["id"]} for c in creds],
            "userVerification": "preferred"}


def complete_authentication(credential: dict):
    wa = _wa()
    challenge = _take_challenge(credential.get("challenge", ""), None, "authentication")
    cred_id = b64e(b64d(credential["id"]))
    row = db.query_one("SELECT * FROM passkey_credentials WHERE id = ?", (cred_id,))
    if not row:
        raise ValueError("Unknown credential")
    verification = wa["verify_authentication_response"](
        credential=credential,
        expected_challenge=b64d(challenge),
        expected_origin=f"https://{settings.rp_id}",
        expected_rp_id=settings.rp_id,
        credential_public_key=b64d(row["public_key"]),
        credential_current_sign_count=row["sign_count"],
    )
    db.execute("UPDATE passkey_credentials SET sign_count = ? WHERE id = ?",
               (verification.new_sign_count, cred_id))
    return db.query_one("SELECT * FROM users WHERE id = ?", (row["user_id"],))


def _store_challenge(challenge: str, user_id: int | None, kind: str) -> None:
    expires = datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_TTL_MIN)
    db.execute(
        "INSERT INTO passkey_challenges(challenge, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
        (challenge, user_id, kind, expires.isoformat()),
    )


def _take_challenge(challenge: str, user_id: int | None, kind: str) -> str:
    row = db.query_one(
        "SELECT * FROM passkey_challenges WHERE challenge = ? AND kind = ? AND expires_at > ?",
        (challenge, kind, datetime.now(timezone.utc).isoformat()),
    )
    if not row:
        raise ValueError("Challenge expired or unknown")
    db.execute("DELETE FROM passkey_challenges WHERE challenge = ?", (challenge,))
    return challenge
