# KB Seed Directory (M06)

This directory holds documents that get ingested into the eKI Knowledge Base.

## Structure

```
config/kb_seed/
  README.md                # this file
  placeholders/            # synthetic stand-in documents (Bernd-Platzhalter)
  real/                    # real safety officer deliverables go here
```

## Placeholder documents (`placeholders/`)

Six synthetic safety SOPs covering the high-risk areas from Pflichtenheft §4.1:

| File | Topic |
|---|---|
| `01_stunt_sop.md` | Stunts, falls, fights |
| `02_fire_sfx_safety.md` | Fire, pyrotechnics, special FX |
| `03_vehicle_action_guidelines.md` | Vehicle action and stunt driving |
| `04_height_rigging_protocol.md` | Heights, rigging, fall arrest |
| `05_intimacy_coordination_checklist.md` | Intimacy coordination, closed set |
| `06_psychological_briefing_procedure.md` | Psychological briefing and debrief |

**They are NOT rechtsverbindlich.** They are minimal-realistic-looking SOPs so
the KB retrieval can be smoke-tested with plausible relevance scores. Each file
carries a YAML front-matter block and the tag `placeholder` so the seeder can
identify and remove them later.

## Replacing placeholders with real content

When the safety officer delivers real documents, drop them into `real/` (any
mix of `.pdf`, `.md`, `.txt`) and run:

```bash
# Remove all placeholders (real docs stay untouched)
python scripts/seed_kb.py --wipe-placeholders

# Ingest every file in real/
python scripts/seed_kb.py --reseed
```

`--reseed` is idempotent: documents whose SHA-256 hash already exists in the
KB are skipped (409 Conflict suppressed). Re-running is safe.

The seeder talks to the running API over HTTP (`EKI_API_URL`, default
`http://localhost:8000`, plus `EKI_API_KEY`) and needs `requests` on the
machine that runs it. The embedding model must be chosen before the first
seed (`docs/M06_KB_GUIDE.md` §3.1); switching it later means wiping and
re-seeding the whole KB.

**Do not ship real documents.** The contents of `real/` are git-ignored, but
the Dockerfiles copy `config/` into the API and worker images (there is no
`.dockerignore`), so anything left in `real/` during a local image build ends
up in the image. Remove the files from `real/` once they are ingested (the KB
keeps the original text encrypted, see `docs/DATENSCHUTZ.md`).

## Re-creating the seeded test KB from scratch

```bash
python scripts/seed_kb.py --wipe-placeholders
python scripts/seed_kb.py --seed-placeholders
python scripts/seed_kb.py --status
```
