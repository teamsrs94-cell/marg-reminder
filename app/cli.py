"""Emergency tools, run on the server:
  docker compose exec marg-reminder python -m app.cli reset-password you@example.com 'NewPassw0rd'
"""
import sys

from sqlalchemy import select

from .config import Settings
from .db import make_engine, make_session_factory
from .models import User
from .security import hash_password


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "reset-password":
        print(__doc__)
        return 2
    email, password = argv[1].strip().lower(), argv[2]
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        return 1
    Session = make_session_factory(make_engine(Settings.from_env().database_url))
    with Session() as db:
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            print(f"No user with email {email}")
            return 1
        user.password_hash = hash_password(password)
        db.commit()
    print(f"Password updated for {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
