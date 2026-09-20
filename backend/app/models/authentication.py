"""Authentication input shared by independent HTTP applications."""
from pydantic import BaseModel, Field

class LoginWithPasswordRequest(BaseModel):
    phone: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=1, max_length=128)
