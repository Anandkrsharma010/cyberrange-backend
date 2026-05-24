import os
import asyncio
import subprocess
import asyncpg

async def main():
    db_url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("MIGRATION_DATABASE_URL or DATABASE_URL not set in environment.")
        return

    # Convert sqlalchemy asyncpg URL to standard postgres connection URL if needed
    # postgresql+asyncpg://postgres:postgres@db:5432/cyberrange -> postgresql://postgres:postgres@db:5432/cyberrange
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    print(f"Connecting to database: {db_url.split('@')[-1]}")
    
    # Wait for database to accept connections
    retries = 30
    conn = None
    for i in range(retries):
        try:
            conn = await asyncpg.connect(db_url)
            print("Connected successfully to PostgreSQL!")
            break
        except Exception as e:
            print(f"Waiting for database to start... ({i+1}/{retries}) Error: {e}")
            await asyncio.sleep(2)
    else:
        print("Failed to connect to database after retries. Exiting.")
        exit(1)

    # Check if 'users' table exists
    table_exists = await conn.fetchval("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_schema = 'public' 
              AND table_name = 'users'
        );
    """)

    if not table_exists:
        print("Database is empty. Applying baseline schemas...")
        
        # In order:
        sql_files = [
            "backend/infrastructure/db_schema.sql",
            "backend/infrastructure/002_no_schema_changes_needed.sql",
            "backend/infrastructure/003_deployment_members.sql",
            "backend/infrastructure/004_rename_role.sql",
            "backend/infrastructure/005_token_audit_log.sql"
        ]
        
        for sql_file in sql_files:
            if os.path.exists(sql_file):
                print(f"Executing: {sql_file}")
                with open(sql_file, "r") as f:
                    sql_content = f.read()
                # Run the sql statements
                async with conn.transaction():
                    await conn.execute(sql_content)
            else:
                print(f"Warning: sql file not found: {sql_file}")
    else:
        print("Database schema already initialized.")

    # Seed the subnet tracker
    print("Seeding subnet tracker...")
    async with conn.transaction():
        await conn.execute("""
            INSERT INTO subnet_tracker (id, last_assigned_octet) 
            VALUES ('counter', 1) 
            ON CONFLICT DO NOTHING;
        """)

    await conn.close()

    # Run Alembic migrations
    print("Running Alembic migrations (upgrade head)...")
    try:
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        print("Alembic migrations completed successfully.")
    except Exception as e:
        print(f"Alembic migration failed: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
