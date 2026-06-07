import os
import sys
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
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
        print("Alembic migrations completed successfully.")
    except Exception as e:
        print(f"Alembic migration failed: {e}")
        exit(1)

    # Seed the default labs
    print("Seeding default labs in the database...")
    try:
        conn = await asyncpg.connect(db_url)
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO content_items (id, type, title, description, difficulty, duration_minutes, is_active, metadata, visibility)
                VALUES 
                  ('a3e2ee8b-70bb-48f1-8f5c-8975a5e3d74c', 'lab', 'Active Directory Basics', 'Learn the fundamentals of Active Directory, GPO, and Kerberos attacks.', 'Beginner', 240, true, '{"slug": "active-directory-basics", "lab_type": "windows", "feature_chips": ["Active Directory", "Kerberoasting", "BloodHound"]}', 'public'),
                  ('b7e66c0d-d421-4f9e-a89c-5b23e7f80da2', 'lab', 'Wazuh SIEM Lab', 'Deploy Wazuh agent, collect logs, and perform threat hunting.', 'Intermediate', 300, true, '{"slug": "wazuh-siem-lab", "lab_type": "wazuh", "feature_chips": ["SIEM", "Wazuh", "Log Analysis", "EDR"]}', 'public'),
                  ('c7e66c0d-d421-4f9e-a89c-5b23e7f80da3', 'lab', 'AWS Cloud Security', 'Explore cloud security, IAM privilege escalation, and VPC isolation.', 'Advanced', 360, true, '{"slug": "aws-cloud-security", "lab_type": "aws", "feature_chips": ["AWS", "VPC Security", "IAM", "Cloud Custodian"]}', 'public')
                ON CONFLICT (id) DO UPDATE SET
                  title = EXCLUDED.title,
                  description = EXCLUDED.description,
                  difficulty = EXCLUDED.difficulty,
                  duration_minutes = EXCLUDED.duration_minutes,
                  is_active = EXCLUDED.is_active,
                  metadata = EXCLUDED.metadata,
                  visibility = EXCLUDED.visibility;

                INSERT INTO product_prices (content_id, amount_minor, currency, is_active)
                VALUES
                  ('a3e2ee8b-70bb-48f1-8f5c-8975a5e3d74c', 5000, 'INR', true),
                  ('b7e66c0d-d421-4f9e-a89c-5b23e7f80da2', 4500, 'INR', true),
                  ('c7e66c0d-d421-4f9e-a89c-5b23e7f80da3', 6000, 'INR', true)
                ON CONFLICT (content_id) DO UPDATE SET
                  amount_minor = EXCLUDED.amount_minor,
                  currency = EXCLUDED.currency,
                  is_active = EXCLUDED.is_active;
            """)
        print("Default labs and prices seeded successfully.")
        await conn.close()
    except Exception as e:
        print(f"Failed to seed default labs: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
