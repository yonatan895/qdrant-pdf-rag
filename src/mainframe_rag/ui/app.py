"""Mainframe Operations Copilot - Interactive Streamlit UI.

Provides an air-gapped terminal interface for mainframe incident analysis,
log/abend inspection, verified citation exploration, and persistent sessions.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import streamlit as st

log = logging.getLogger(__name__)

from mainframe_rag.ui.components.drawer import render_incident_drawer
from mainframe_rag.ui.components.sidebar import render_sidebar
from mainframe_rag.ui.db import (
    add_message,
    get_session_messages,
    init_db,
)
from mainframe_rag.ui.styles import get_theme_css


def _get_env_config() -> tuple[str, str, Path]:
    raw_url = os.getenv("AGENT_URL", os.getenv("AGENT_BASE_URL", "http://localhost:8080")).rstrip("/")
    if raw_url.endswith("/v1"):
        host_url = raw_url[:-3]
        api_v1_url = raw_url
    else:
        host_url = raw_url
        api_v1_url = f"{raw_url}/v1"
    data_dir = Path(os.getenv("UI_DATA_DIR", "/tmp/mainframe_rag_ui"))
    db_path = data_dir / "copilot_sessions.db"
    return host_url, api_v1_url, db_path


def _render_citations_and_hits(citations: list[str], hits: list[dict[str, Any]]) -> None:
    if citations:
        st.markdown("**Verified Manual Citations:**")
        for cite in citations:
            st.markdown(
                f'<div class="citation-card"><span class="citation-badge">CITE</span>{cite}</div>',
                unsafe_allow_html=True,
            )

    if hits:
        with st.expander(f"📚 Retrieved Manual Excerpts ({len(hits)} pages)", expanded=False):
            for i, h in enumerate(hits, start=1):
                title = h.get("title") or h.get("doc_id") or "Manual"
                heading = h.get("heading") or "General"
                page = h.get("page_label") or "?"
                cite_str = h.get("cite") or f"{title} p. {page}"
                st.markdown(f"**[{i}] {cite_str}**")
                st.caption(f"Section: `{heading}` | Type: `{h.get('chunk_type', 'narrative')}`")
                st.code(h.get("text", "")[:600] + ("..." if len(h.get("text", "")) > 600 else ""), language="text")


def main() -> None:
    st.set_page_config(
        page_title="Mainframe Operations Copilot",
        page_icon="🖥️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    host_url, api_v1_url, db_path = _get_env_config()
    init_db(db_path)

    session_id = render_sidebar(db_path, host_url)

    # Invalidate session cache if session changed
    active_theme = st.session_state.get("theme", "Modern Engineering Dark")
    st.markdown(get_theme_css(active_theme), unsafe_allow_html=True)

    st.markdown("### 🖥️ Mainframe Operations Copilot")
    st.caption("Air-Gapped SOTA Reasoning Engine | Grounded IBM/Vendor Technical Documentation")

    # Render incident context drawer
    attached_splunk_context, prod_filter, ver_filter = render_incident_drawer()

    # Load existing dialogue
    messages = get_session_messages(db_path, session_id)

    # Display dialogue history
    for m in messages:
        with st.chat_message(m["role"]):
            if m.get("splunk_context"):
                with st.expander("📋 Attached Incident Spool / Abend Dump", expanded=False):
                    st.code(m["splunk_context"], language="text")

            st.markdown(m["content"])
            _render_citations_and_hits(m.get("citations", []), m.get("hits", []))

    # User chat input
    user_prompt = st.chat_input("Enter message code, abend symptom, or question (e.g., IEA500I, S0C4 in COBOL)...")
    if user_prompt:
        # 1. Display user prompt immediately
        with st.chat_message("user"):
            if attached_splunk_context:
                with st.expander("📋 Attached Incident Spool / Abend Dump", expanded=False):
                    st.code(attached_splunk_context, language="text")
            st.markdown(user_prompt)

        # 2. Persist user message to SQLite
        add_message(
            db_path,
            session_id,
            role="user",
            content=user_prompt,
            splunk_context=attached_splunk_context,
        )

        # 3. Assemble API messages
        api_messages = []
        for m in messages:
            api_messages.append({"role": m["role"], "content": m["content"]})
        api_messages.append({"role": "user", "content": user_prompt})

        payload = {
            "messages": api_messages,
            "stream": True,
            "splunk_context": attached_splunk_context,
            "product": prod_filter,
            "version": ver_filter,
        }

        # 4. Stream response from agent endpoint
        with st.chat_message("assistant"):
            full_response_text = ""
            final_citations: list[str] = []
            final_hits: list[dict[str, Any]] = []

            def stream_generator() -> Iterator[str]:
                nonlocal full_response_text, final_citations, final_hits
                target_url = f"{api_v1_url}/chat/completions"
                timeout = httpx2.Timeout(120.0, connect=5.0)

                try:
                    with httpx2.Client(timeout=timeout) as client, client.stream("POST", target_url, json=payload) as response:
                        if response.status_code != 200:
                            err_msg = f"\n\n**Error {response.status_code}:** Failed to connect to reasoning agent."
                            full_response_text += err_msg
                            yield err_msg
                            return

                        for line in response.iter_lines():
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data_str = line[6:].strip()
                                if data_str == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                except json.JSONDecodeError:
                                    continue

                                choices = chunk.get("choices", [])
                                if not choices:
                                    continue
                                choice = choices[0]
                                delta = choice.get("delta", {})
                                delta_content = delta.get("content")
                                if delta_content:
                                    full_response_text += delta_content
                                    yield delta_content

                                if choice.get("citations"):
                                    final_citations = choice["citations"]
                                if choice.get("hits"):
                                    final_hits = choice["hits"]

                except Exception as exc:  # noqa: BLE001 — show friendly streaming error to operator
                    log.error("Streaming error from agent: %s", exc)
                    err_msg = "\n\n**Agent Communication Fault:** Unable to retrieve response from reasoning agent."
                    full_response_text += err_msg
                    yield err_msg

            # Stream text into chat UI
            st.write_stream(stream_generator())

            # Render citation cards below streamed text
            _render_citations_and_hits(final_citations, final_hits)

            # 5. Persist assistant response to SQLite
            add_message(
                db_path,
                session_id,
                role="assistant",
                content=full_response_text,
                citations=final_citations,
                hits=final_hits,
            )


if __name__ == "__main__":
    main()
