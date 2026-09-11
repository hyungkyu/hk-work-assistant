-- 0009_org_advisor
-- Whose lab a Virtual Lab student is in.
--
-- The external roster tab carries an advisor per student (column G, "소속 학교
-- 연구실 지도교수님"), and that is what "연구실" means for these people: the
-- lab is the advisor's lab. The internal tab's Virtual Lab paths carry
-- project names instead -- Modular VLA, Allex, 3D/4D Perception -- which are
-- a different axis and cannot answer "whose lab is this person in".
--
-- Added as its own migration rather than by editing 0007, whose checksum is
-- already recorded wherever it has been applied. A migration that has run is
-- history.

ALTER TABLE org_person_state
    ADD COLUMN IF NOT EXISTS advisor text;

COMMENT ON COLUMN org_person_state.advisor IS
    'The advisor a Virtual Lab student is under, verbatim from the roster '
    '(honorific included). Names the lab node on the org chart.';
