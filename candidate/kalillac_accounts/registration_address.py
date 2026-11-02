"""V2 deliverable addresses use the selected Resend gateway's exact contract."""
from .resend_gateway import ADDRESS

def normalize_registration_email(value):
    # Keep legacy account/login normalization separate and unchanged.
    from .router import normalize_email
    email=normalize_email(value)
    return email if email is not None and ADDRESS.fullmatch(email) else None
