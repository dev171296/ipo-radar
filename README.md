# IPO Radar

Automated analysis of Indian IPOs (NSE + BSE, mainboard and SME).
Collects the numbers and the prospectus, scores them on published rules,
has two AI models judge them independently, and grades its own predictions
against what actually happened.

Runs entirely on free tiers. Personal research tool — **not investment advice**.

## Status

Phase 1 — verifying data sources from a GitHub Actions runner.

## Design docs

- Blueprint (PRD + solution design)
- Architecture diagrams

## Layout

    probe.py                     one-off source reachability check
    .github/workflows/probe.yml  runs the probe on demand
