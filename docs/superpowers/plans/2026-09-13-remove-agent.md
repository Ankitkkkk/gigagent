# Remove saved agents

User asks for a Remove option that removes terminated agents from the list and
stops their tmux session. Extend the current F3 action menu and authenticated
workspace API. Removal also works for running agents after an explicit in-app
confirmation. Keep session messages, provider transcripts, and diagnostic logs.

1. Add bounded wrapper cleanup based on a retained Popen handle, or verified
   Linux process identity with pidfd for an orphan after server restart. Never
   signal an unrelated reused PID. Kill only the exact agent-ID tmux session,
   verify it is gone, and retain the row on cleanup failure. Serialize removal
   with resume/stop. Discard pending launcher work and identity/queue files,
   deregister the owned runtime identity, then delete the saved row.
2. Add POST /api/workspaces/{ws}/agents/{agent}/remove with existing session
   authentication and workspace broadcasts. Clear runtime hint/read state only
   when the removed name no longer belongs to another runtime instance.
3. Add F3 / palette Remove agent action for stopped and active agents, a default
   No confirmation explaining stop/removal and retained chats, and controller
   response handling for a workspace rather than an agent record. Return to the
   composer after success; errors/cancellation keep the entry and draft.

Verification: first RED unit tests for cleanup and UI; fake processes/tmux for
unit coverage, temporary inert processes for PID ownership and forced cleanup,
isolated HTTP/tmux integration where needed, then outer-isolated full suite.
Review the destructive lifecycle path independently. Do not remove any actual
user agent during QA. Keep Claude informed via AGENT_MESSAGES.md.

Completed: all three steps implemented. Independent review clear after fixes for
spawn/remove serialization, transient empty Linux argv, survivor-name retention,
and store-save rollback. Focused17 pass; final outer-isolated full discovery943
total,941 pass,2 expected skips,151.295s (2026-09-13). Log:
/tmp/yapp-remove-final-suite.log. One pre-existing Help-test scheduling race
was fixed with a bounded state wait. Live server/CLI restart remains an activation
step; no actual user agents were removed during verification.
