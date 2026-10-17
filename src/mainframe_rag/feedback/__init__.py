"""Console answer feedback (issue #780): a protected store of finalized answer
records and the human ratings attached to them, keyed by the agent's
server-minted request_id. Off unless FEEDBACK_ENABLED; never on the answer's
critical path."""
