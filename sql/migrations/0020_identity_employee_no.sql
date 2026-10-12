-- 0020_identity_employee_no
--
-- Let a 사번 be stored as an identity.
--
-- HK, 2026-10-12: 제일 첫줄에 사번을 넣었어.
--
-- The code that reads the column shipped without this, so the first sync
-- against the new sheet failed on the constraint rather than on anything it
-- was trying to decide:
--
--   org_identity_kind_check / (employee_no, 100001, p_e65824f284d4, ...)
--
-- That is the constraint doing its job -- an identity kind nobody declared
-- is how a typo becomes a table of junk -- and the fix is to declare the
-- kind, not to loosen the rule.

ALTER TABLE org_identity DROP CONSTRAINT IF EXISTS org_identity_kind_check;
ALTER TABLE org_identity
    ADD CONSTRAINT org_identity_kind_check
    CHECK (kind IN (
        'employee_no',
        'email_official', 'email_personal', 'email_school',
        'github', 'slack', 'notion', 'slurm'
    ));
