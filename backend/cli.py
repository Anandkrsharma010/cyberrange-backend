# backend/cli.py
"""
CyberRange management CLI.

Usage (always run from project root):
    python3 -m backend.cli promote-admin <email>
    python3 -m backend.cli list-users

promote-admin: promotes the user with the given email to sys_admin.
Requires direct shell access to the server — no HTTP endpoint needed.
"""

import asyncio
import sys

from sqlalchemy import text
from backend.pg import get_session


async def promote_admin(email: str) -> None:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id, email, role FROM users WHERE email = :email"),
            {"email": email.strip().lower()},
        )
        row = result.fetchone()
        if not row:
            print(f"ERROR: No user found with email '{email}'.")
            print("The user must log in at least once before being promoted.")
            sys.exit(1)

        if row.role == "sys_admin":
            print(f"'{email}' is already a sys_admin. Nothing to do.")
            sys.exit(0)

        await session.execute(
            text("""
                UPDATE users SET role = 'sys_admin', updated_at = now()
                WHERE email = :email
            """),
            {"email": email.strip().lower()},
        )
        await session.commit()
        print(f"SUCCESS: {email} (id={row.id}) promoted to sys_admin.")


async def list_users() -> None:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id, email, role, is_active FROM users ORDER BY created_at ASC")
        )
        rows = result.fetchall()
        if not rows:
            print("No users found.")
            return
        print(f"{'email':<40} {'role':<15} {'active':<8} {'id'}")
        print("-" * 90)
        for r in rows:
            print(f"{r.email:<40} {r.role:<15} {str(r.is_active):<8} {r.id}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 -m backend.cli promote-admin <email>")
        print("  python3 -m backend.cli list-users")
        sys.exit(1)

    command = sys.argv[1]

    if command == "promote-admin":
        if len(sys.argv) < 3:
            print("ERROR: email argument required.")
            print("  python3 -m backend.cli promote-admin <email>")
            sys.exit(1)
        asyncio.run(promote_admin(sys.argv[2]))

    elif command == "list-users":
        asyncio.run(list_users())

    else:
        print(f"ERROR: Unknown command '{command}'.")
        sys.exit(1)


if __name__ == "__main__":
    main()