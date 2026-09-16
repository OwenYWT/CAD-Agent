# Review Memory

Each file records reusable facts verified during one review. Name files:

```text
YYYY-MM-DD-<head>-<review-id>.md
```

Never overwrite an existing file. Use a unique agent/task-oriented `review-id` when reviews share the same date and HEAD.

```markdown
# Review Memory: YYYY-MM-DD <head> <review-id>

- Reviewer agent:
- Review ID:
- Reviewed at:
- Range:
- Working tree state:
- Change class:
- Feature domains:
- Changed implementation paths:
- Relevant symbols/contracts:
- Tests actually run:
- Evidence classification:
- Findings and limits:
- Stable knowledge candidates:
```

Review memory is append-only evidence, not proof that current code remains correct.
