An unattended run of the tile pipeline raised issue #ISSUE.
Its body (and its latest comment, if it has comments) is the run's merged summary:
failures grouped by cause, each with its class (transient, nodata, upstream, bug),
a signature, an example message and tiles.

Read pipeline/ARCHITECTURE.md first, then the code the failures point to.
For each cause decide which it is:
- an outage (network, a source down for a while): no code change; say so and say
  what would show that it is not passing;
- a source that changed (format, URL, a 4xx that is ours to fix): fix the importer
  or product that reads it;
- a bug: fix it.

Make the smallest change that fixes the cause, add or extend a test in
pipeline/tests that fails without the fix, run
`cd pipeline && python -m unittest discover -s tests`, and open a pull request
that references the issue and explains the cause in two or three sentences.
Never weaken a validation or widen an error class to make a failure disappear;
never touch data/tiles or credentials. If no change is warranted, comment on the
issue with your reasoning instead.
