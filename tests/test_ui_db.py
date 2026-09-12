"""Unit tests for UI SQLite persistence layer (sessions, messages, export)."""

from __future__ import annotations

from mainframe_rag.ui.db import (
    add_message,
    create_session,
    delete_session,
    export_markdown,
    get_session_messages,
    init_db,
    list_sessions,
)


def test_db_session_lifecycle(tmp_path):
    db_file = tmp_path / "test_sessions.db"
    init_db(db_file)

    # 1. Create sessions
    s1 = create_session(db_file, title="New Incident")
    s2 = create_session(db_file, title="Custom Title")

    sessions = list_sessions(db_file)
    assert len(sessions) == 2
    session_ids = {s["id"] for s in sessions}
    assert s1 in session_ids
    assert s2 in session_ids

    # 2. Add messages to s1
    msg1_id = add_message(
        db_file,
        s1,
        role="user",
        content="What is abend S0C4 in COBOL program?",
        splunk_context="//STEP1 EXEC PGM=TEST",
    )
    assert msg1_id > 0

    # Auto-title check: title should update from default "New Incident" to user question
    s1_updated = next(s for s in list_sessions(db_file) if s["id"] == s1)
    assert "What is abend S0C4 in COBOL program?" in s1_updated["title"]

    # Assistant message with citations & hits
    hits = [
        {
            "title": "z/OS MVS System Codes",
            "heading": "S0C4",
            "page_label": "2-45",
            "cite": "SA38-0665-00 z/OS MVS System Codes p. 2-45",
            "text": "Explanation of protection exception...",
            "chunk_type": "message",
        }
    ]
    citations = ["SA38-0665-00 z/OS MVS System Codes p. 2-45"]
    msg2_id = add_message(
        db_file,
        s1,
        role="assistant",
        content="Abend S0C4 is a protection exception.",
        citations=citations,
        hits=hits,
    )
    assert msg2_id > msg1_id

    # 3. Retrieve messages
    messages = get_session_messages(db_file, s1)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["splunk_context"] == "//STEP1 EXEC PGM=TEST"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["citations"] == citations
    assert len(messages[1]["hits"]) == 1
    assert messages[1]["hits"][0]["heading"] == "S0C4"

    # 4. Export Markdown
    report = export_markdown(db_file, s1)
    assert "# Incident Analysis:" in report
    assert "What is abend S0C4 in COBOL program?" in report
    assert "Operator (" in report
    assert "Mainframe Copilot (" in report
    assert "Attached Incident Context" in report
    assert "SA38-0665-00" in report

    # 5. Delete session
    delete_session(db_file, s1)
    remaining = list_sessions(db_file)
    assert len(remaining) == 1
    assert remaining[0]["id"] == s2
    assert get_session_messages(db_file, s1) == []
