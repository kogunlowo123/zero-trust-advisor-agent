"""Zero Trust Advisor Agent - Auth Service."""

from datetime import datetime, timedelta, timezone
from jose import jwt
from src.config import get_settings

settings = get_settings()


class AuthService:
    """JWT authentication service."""

    @staticmethod
    def create_access_token(user_id: str, email: str) -> str:
        payload = {"sub": user_id, "email": email, "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiration_hours)}
        return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
