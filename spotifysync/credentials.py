"""Keyring-backed credential and token storage."""

import json

import keyring

from .config import KEYRING_SERVICE, logger

SPOTIFY_ID_KEY = "SPOTIFY_CLIENT_ID"
SPOTIFY_SECRET_KEY = "SPOTIFY_CLIENT_SECRET"
SPOTIFY_TOKEN_KEY = "spotify_token"
TIDAL_SESSION_KEY = "tidal_session"

ALL_KEYS = (SPOTIFY_ID_KEY, SPOTIFY_SECRET_KEY, SPOTIFY_TOKEN_KEY, TIDAL_SESSION_KEY)


def get(key):
    try:
        return keyring.get_password(KEYRING_SERVICE, key)
    except Exception as e:
        logger.warning(f"Keyring read error ({key}): {e}")
        return None


def set(key, value):
    try:
        keyring.set_password(KEYRING_SERVICE, key, value)
        return True
    except Exception as e:
        logger.error(f"Keyring write error ({key}): {e}")
        return False


def delete(key):
    try:
        keyring.delete_password(KEYRING_SERVICE, key)
    except Exception:
        pass


def delete_all():
    for key in ALL_KEYS:
        delete(key)


def get_spotify_credentials():
    return get(SPOTIFY_ID_KEY), get(SPOTIFY_SECRET_KEY)


def get_json(key):
    raw = get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_json(key, data):
    return set(key, json.dumps(data))
