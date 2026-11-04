"""Shared game application parameters, intentionally distributed for zero-setup QR export.

These identify/sign the game application; they are NOT a user's login token,
MAC key, password, cookie or the developer console Server Secret.
No personal account or machine configuration belongs in this module.
"""

CLIENT_ID = 'o8yjihhzayqwweaopa'
CLIENT_TOKEN = '0eOYzfBraRTfzkyssjllnOeWO6DUed0SK9O8wfOi'


def load_profile():
    return {'client_id': CLIENT_ID, 'client_token': CLIENT_TOKEN}
