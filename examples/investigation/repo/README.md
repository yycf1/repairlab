# Label parser

`parse_labels(text)` reads comma-separated labels. Trim whitespace around each
label, discard empty labels, and preserve both case and input order. Repeated
labels are meaningful and must be preserved. An empty input returns an empty list.

This is a small, deliberately incomplete, self-written repository used to verify
RepairLab's investigation loop. It is not an external open-source benchmark.
