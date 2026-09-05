# job-hound ingest timer

`job_ingest.py` drains the lead-submission spool: a UI drops a posting URL into
`$JOB_INBOX_DIR/pending`, this job picks it up, fetches the JD, runs the fit
gate, and auto-drafts a package when the gate comes back clean. It is the only
writer of `jobs.db` in that flow.

The timer runs it every 5 minutes.

## Environment

Set these in the host env file (`~/.job-hound/job-hound.env`, mode 0600, the
same one the systemd units load with `EnvironmentFile=`):

- `ANTHROPIC_API_KEY`: required. This path calls the model.
- `JOB_DB`: path to the one canonical `jobs.db`.
- `JOB_INBOX_DIR`: the spool directory. Must match what the submitting UI
  writes to. `pending/` and `processed/` live under it.
- `DISCORD_WEBHOOK_URL`: optional, reuses job-hound's existing webhook.
- `JOB_MASTER`: optional. Defaults to `~/job-hound/master_resume.yaml`.
- `JOB_PDF`: set to `off` if LibreOffice (`soffice`) is not installed on the
  host. DOCX still writes.

## Install

    cp ~/job-hound/deploy/job-ingest.{service,timer} ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now job-ingest.timer
    systemctl --user list-timers | grep job-ingest

## Logs

    journalctl --user -u job-ingest -f

## Crontab fallback

If you are not using systemd user units:

    */5 * * * * flock -n $HOME/.job-hound/job-ingest.lock $HOME/job-hound/bin/ingest-queue.sh >> $HOME/logs/job-hound/job-ingest.log 2>&1

`bin/ingest-queue.sh` reads its environment from the systemd `EnvironmentFile`
or the crontab environment, so under cron you have to source the env file
yourself or set the variables in the crontab.

## Note on the write lock

This timer takes the `jobs.db` write lock every five minutes. Ad-hoc `sqlite3`
queries need `-cmd ".timeout 5000"` or they fail with `database is locked` at
roughly that cadence. See `docs/single-source-of-truth.md`.

## Hard rule

This path prepares documents. It never submits an application, fills an
external form, or logs into a job site.
