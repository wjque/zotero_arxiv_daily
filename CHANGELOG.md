# Changelog

## v0.1.0 - 2026-08-01

### Added

- Local-first incremental Zotero synchronization and deterministic protected interest profiles.
- Rate-limited, resumable arXiv discovery; bounded DeepSeek recommendations; and validated diversity selection.
- Encrypted static GitHub Pages output, browser-local feedback, and same-repository feedback Issue ingestion.
- Scheduled/manual GitHub Actions publishing with protected state, concurrency control, and safe run diagnostics.

### Security

- Raw Zotero notes, annotations, and PDF content remain local.
- Published recommendation data is encrypted by default and model/provider failures do not expose secrets, prompts, or response bodies.
