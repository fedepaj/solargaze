The nightly tile pipeline just finished (workflow run RUN_URL). Its merged summary is
the latest comment on issue #LOG. ALERT_LINE GAP_LINE

When you are done, post exactly one comment on issue #LOG, in Italian, starting with
@OWNER, of at most twelve short lines:
- what was built (tiles and countries), what failed and the cause in plain words, what you
  did about it (link the pull request if you opened one) or why nothing was needed;
- the gaps: the open issues labelled gap (`gh issue list --label gap`) — how many, which
  one was worked on tonight and with what result (a pull request to review, or why no
  source was found), and which are blocked waiting for a person;
- what the next run will take on.
Numbers, not adjectives. Use `gh issue comment LOG --body-file <file>`.
