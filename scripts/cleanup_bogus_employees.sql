-- Cleanup: remove bogus PLI employee placeholder rows created by mistakenly
-- importing an equipment-format Excel file via the employee import endpoint.
--
-- Safe to run on either SQLite (local dev) or PostgreSQL (Railway prod).
--
-- Predicate for "bogus":
--   * company = 'PLI'
--   * name is null/blank, OR matches a known equipment header / company code
--   * no license info
--   * no profile fields filled (telefono, email, puesto, fecha_nacimiento, fecha_contratacion)
--   * no employee_permits row with a real expiration_date
--
-- Usage:
--   1. Run the SELECT block below first and eyeball the output.
--   2. If the list looks right, run the BEGIN..COMMIT block to delete.
--   3. If anything looks wrong, do NOT run the DELETE block — investigate first.

-- ──────────────────────────────────────────────────────────────────────
-- STEP 1 — preview the rows that will be deleted
-- ──────────────────────────────────────────────────────────────────────
SELECT id, name, company, license_number, license_expiration, area, status
FROM employees
WHERE company = 'PLI'
  AND (
        name IS NULL
     OR TRIM(name) = ''
     OR LOWER(TRIM(name)) IN (
            'company', 'compañia', 'compania', 'titular',
            'lb', 'pli', 'personal', 'unidad', 'unit'
        )
  )
  AND (license_number IS NULL OR license_number = '')
  AND license_expiration IS NULL
  AND (telefono IS NULL OR telefono = '')
  AND (email IS NULL OR email = '')
  AND (puesto IS NULL OR puesto = '')
  AND fecha_nacimiento IS NULL
  AND fecha_contratacion IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM employee_permits ep
        WHERE ep.employee_id = employees.id
          AND ep.expiration_date IS NOT NULL
  );

-- ──────────────────────────────────────────────────────────────────────
-- STEP 2 — delete (wrapped in a transaction so you can ROLLBACK if needed)
-- ──────────────────────────────────────────────────────────────────────
BEGIN;

-- Child rows first: the ORM-level cascade in models.py doesn't fire on raw SQL.
DELETE FROM employee_permits
WHERE employee_id IN (
    SELECT id FROM employees
    WHERE company = 'PLI'
      AND (
            name IS NULL
         OR TRIM(name) = ''
         OR LOWER(TRIM(name)) IN (
                'company', 'compañia', 'compania', 'titular',
                'lb', 'pli', 'personal', 'unidad', 'unit'
            )
      )
      AND (license_number IS NULL OR license_number = '')
      AND license_expiration IS NULL
      AND (telefono IS NULL OR telefono = '')
      AND (email IS NULL OR email = '')
      AND (puesto IS NULL OR puesto = '')
      AND fecha_nacimiento IS NULL
      AND fecha_contratacion IS NULL
      AND NOT EXISTS (
            SELECT 1 FROM employee_permits ep
            WHERE ep.employee_id = employees.id
              AND ep.expiration_date IS NOT NULL
      )
);

DELETE FROM employees
WHERE company = 'PLI'
  AND (
        name IS NULL
     OR TRIM(name) = ''
     OR LOWER(TRIM(name)) IN (
            'company', 'compañia', 'compania', 'titular',
            'lb', 'pli', 'personal', 'unidad', 'unit'
        )
  )
  AND (license_number IS NULL OR license_number = '')
  AND license_expiration IS NULL
  AND (telefono IS NULL OR telefono = '')
  AND (email IS NULL OR email = '')
  AND (puesto IS NULL OR puesto = '')
  AND fecha_nacimiento IS NULL
  AND fecha_contratacion IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM employee_permits ep2
        WHERE ep2.employee_id = employees.id
          AND ep2.expiration_date IS NOT NULL
  );

COMMIT;
