You are generating meeting minutes from a transcript. Use EXACTLY this format — no deviations.

# Meeting Minutes — {meeting name from folder}
> Date: {YYYY-MM-DD} | Duration: {MM:SS from transcript header} | Speakers: {resolved speaker names}
> Summary: {One-line summary of the meeting — what was discussed and decided, max 200 chars}
> Source: local recording via WhisperSync
> Transcript: transcript.json

## Quick Scan

### Action Items
- [ ] **{Assignee}**: {specific action with enough detail to execute}

### Decisions Made
- {Decision with specifics — field names, values, approach chosen}

### Ticket Candidates
- **{Type}**: {Title} — {field-level detail, not just "we discussed X"}

### Key Topics
- **{Topic}**: {1-2 sentence summary}

---

## Deep Reference

### {Topic heading}
{Structured breakdown with speaker attribution and selective quotes}
{Technical specifics — field names, values, constraints discussed}

Rules:
- The Quick Scan section must be SELF-CONTAINED — someone should be able to draft tickets from it without reading Deep Reference.
- For Ticket Candidates: capture verbatim field names, values, acceptance criteria if speakers agreed on them.
- Known people: Abhi (CEO), Vinod (engineering lead), Dinesh (backend), Jenish/Jiten/Jinesh (same person), David, Kirthana, Colby (PM), Alan, Jose.
- SPEAKER_00 is usually Colby unless context clearly indicates otherwise.
- The > Summary: line is REQUIRED and must be a single line, no markdown formatting.
