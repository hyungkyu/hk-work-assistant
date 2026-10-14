-- 0022_cv
--
-- 인사정보 상세 in the database: 원본 -> 디비 -> 화면.
--
-- HK, 2026-10-14: 이 정보들은 db에 넣고, 클릭하면 디비에서 조회해서 보여지는
-- 식으로 ... 그러면 난 원본에서 잘못 읽힌 것을 디비에서 수정할 수 있겠지.
-- 디비가 수정되면 삼성전자 -> 삼성, 다음카카오 -> 카카오 -> 다음이 하나로
-- 합쳐질 수 있을 것 같고.
--
-- The analysis file per 사번 is the original. It is loaded into these tables
-- when it is newer than what was loaded, unless somebody has edited that
-- person here: an edit is never overwritten by a re-read.
--
-- Companies, schools, advisors, projects and papers are entities. Merging
-- two is pointing one at the other (canonical_id); every row keeps the name
-- it was read with, so a merge can be undone.

CREATE TABLE IF NOT EXISTS cv_entity (
    entity_id    bigserial PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('org', 'school', 'advisor', 'project', 'paper')),
    name         text NOT NULL,
    canonical_id bigint REFERENCES cv_entity (entity_id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS cv_profile (
    employee_no  text PRIMARY KEY,
    built_at     text,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    source_mtime double precision,
    edited_at    timestamptz,
    sources      jsonb NOT NULL DEFAULT '{}',
    fetched      jsonb NOT NULL DEFAULT '{}',
    headline     text,
    summary      text,
    metrics      jsonb NOT NULL DEFAULT '{}',
    flags        jsonb NOT NULL DEFAULT '[]',
    original     jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_education (
    row_id      bigserial PRIMARY KEY,
    employee_no text NOT NULL REFERENCES cv_profile (employee_no) ON DELETE CASCADE,
    ord         int NOT NULL DEFAULT 0,
    level       text NOT NULL CHECK (level IN (
        '고등학교', '대학교', '대학원 (석사)', '대학원 (박사)', '대학원 (석박사통합과정)',
        '대학원 (석사과정)', '대학원 (박사과정)')),
    school_id   bigint REFERENCES cv_entity (entity_id),
    major       text,
    degree      text,
    advisor_id  bigint REFERENCES cv_entity (entity_id),
    lab         text,
    date_from   text,
    date_to     text,
    note        jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS cv_experience (
    row_id      bigserial PRIMARY KEY,
    employee_no text NOT NULL REFERENCES cv_profile (employee_no) ON DELETE CASCADE,
    ord         int NOT NULL DEFAULT 0,
    org_id      bigint REFERENCES cv_entity (entity_id),
    title       text,
    date_from   text,
    date_to     text,
    note        jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS cv_project (
    row_id        bigserial PRIMARY KEY,
    experience_id bigint NOT NULL REFERENCES cv_experience (row_id) ON DELETE CASCADE,
    ord           int NOT NULL DEFAULT 0,
    project_id    bigint REFERENCES cv_entity (entity_id),
    role          text,
    note          jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS cv_publication (
    row_id      bigserial PRIMARY KEY,
    employee_no text NOT NULL REFERENCES cv_profile (employee_no) ON DELETE CASCADE,
    ord         int NOT NULL DEFAULT 0,
    paper_id    bigint REFERENCES cv_entity (entity_id),
    venue       text,
    year        int,
    position    text,
    citations   int,
    award       text,
    summary     text,
    url         text,
    authors     text
);

CREATE TABLE IF NOT EXISTS cv_etc (
    row_id      bigserial PRIMARY KEY,
    employee_no text NOT NULL REFERENCES cv_profile (employee_no) ON DELETE CASCADE,
    ord         int NOT NULL DEFAULT 0,
    label       text NOT NULL,
    items       jsonb NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS cv_education_school_idx ON cv_education (school_id);
CREATE INDEX IF NOT EXISTS cv_education_advisor_idx ON cv_education (advisor_id);
CREATE INDEX IF NOT EXISTS cv_experience_org_idx ON cv_experience (org_id);
CREATE INDEX IF NOT EXISTS cv_project_project_idx ON cv_project (project_id);
CREATE INDEX IF NOT EXISTS cv_publication_paper_idx ON cv_publication (paper_id);
