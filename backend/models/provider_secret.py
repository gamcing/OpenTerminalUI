"""Encrypted provider configuration persisted by the experimental store."""
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.shared.db import Base


class ProviderSecret(Base):
    __tablename__ = "provider_secrets"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
