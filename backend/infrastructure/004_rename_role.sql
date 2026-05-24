BEGIN;

-- Rename 'student' → 'participant'
UPDATE users
SET role = 'participant', updated_at = now()
WHERE role = 'student';

-- Rename 'admin' → 'sys_admin'
UPDATE users
SET role = 'sys_admin', updated_at = now()
WHERE role = 'admin';

-- Update the CHECK constraint on the role column
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;

ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role = ANY (ARRAY['participant', 'course_admin', 'sys_admin']));

COMMIT;

-- Verify
SELECT role, COUNT(*) FROM users GROUP BY role ORDER BY role;