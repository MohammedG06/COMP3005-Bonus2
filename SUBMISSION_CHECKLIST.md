# Submission checklist

## Before you submit
- [ ] Rewrite every explanation in REPORT_M1, REPORT_M2, REPORT_M4 and the DESIGN_*.md files in your own words. You are examined on all of it.
- [ ] Read each source listed (the links work) so you can answer the one reading question per module.
- [ ] Read DESIGN_LOG.md. Every entry is real. Add your own if you hit your own problems.
- [ ] Confirm the machine line in REPORT_M1 and REPORT_M2 matches the computer you ran on.
- [ ] If the course dataset and queries are published, re-run the commands in REPORT_M4 and paste the new tables.
- [ ] Tag the commits (`git tag m1`, `m2`, `m3`, `m4`) and push.

## Assumptions
- Record size limit: the spec says 4088, but a record also needs a 4 byte slot, so the code allows 4084. Documented in DESIGN_M1.md.
- Dates: the handout gives only week ranges (1-2, 3-5, 6-7, 8-9, 10), not calendar dates. The course window you gave is Oct 7 to Nov 15.
- REPORT_M4 uses our own demo data (`scripts/make_demo_db.py`), because the course dataset is not in the files we have.
