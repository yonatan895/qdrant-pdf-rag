"""Sidebar component for session management, theme toggle, and incident export."""

from __future__ import annotations

from pathlib import Path

import httpx2
import streamlit as st

from mainframe_rag.ui.db import (
    create_session,
    delete_session,
    export_markdown,
    list_sessions,
)


def check_agent_health(agent_url: str) -> dict[str, str]:
    """Check agent health endpoint."""
    try:
        url = agent_url.rstrip("/") + "/healthz"
        res = httpx2.get(url, timeout=1.5)
        if res.status_code == 200:
            data = res.json()
            status = data.get("status", "ok")
            return {"status": "ok" if status == "ok" else "degraded", "label": "Online" if status == "ok" else "Degraded"}
        return {"status": "error", "label": f"HTTP {res.status_code}"}
    except Exception:  # noqa: BLE001 — connection errors show offline badge
        return {"status": "error", "label": "Offline"}


def render_sidebar(db_path: Path | str, agent_url: str) -> str:
    """Render the sidebar UI and return the active session_id."""
    with st.sidebar:
        st.title("🖥️ Mainframe Copilot")
        st.caption("Air-Gapped SOTA Technical Assistant")

        # Theme Selector
        theme = st.selectbox(
            "Terminal Interface Style",
            options=["IBM 3270 Phosphor Terminal", "Modern Engineering Dark"],
            index=0 if st.session_state.get("theme", "").startswith("IBM") else 1,
            key="theme_selector",
        )
        st.session_state["theme"] = theme

        st.divider()

        # Session Actions
        col_new, _ = st.columns([1, 1])
        with col_new:
            if st.button("➕ New Incident", use_container_width=True):
                new_id = create_session(db_path, title="New Incident")
                st.session_state["current_session_id"] = new_id
                st.rerun()

        # Session List
        sessions = list_sessions(db_path)
        if not sessions:
            new_id = create_session(db_path, title="New Incident")
            st.session_state["current_session_id"] = new_id
            sessions = list_sessions(db_path)

        current_id = st.session_state.get("current_session_id")
        if not current_id or not any(s["id"] == current_id for s in sessions):
            current_id = sessions[0]["id"]
            st.session_state["current_session_id"] = current_id

        st.subheader("Incident Sessions")
        for s in sessions:
            is_active = s["id"] == current_id
            label = f"▶ {s['title']}" if is_active else s["title"]
            col_sess, col_del = st.columns([5, 1])
            with col_sess:
                if st.button(label, key=f"sess_{s['id']}", use_container_width=True):
                    st.session_state["current_session_id"] = s["id"]
                    st.rerun()
            with col_del:
                if st.button("✕", key=f"del_{s['id']}", help="Delete session"):
                    delete_session(db_path, s["id"])
                    if s["id"] == current_id:
                        st.session_state.pop("current_session_id", None)
                    st.rerun()

        st.divider()

        # Export Report
        md_export = export_markdown(db_path, current_id)
        st.download_button(
            "📥 Export Incident Log (MD)",
            data=md_export,
            file_name=f"incident_{current_id}.md",
            mime="text/markdown",
            use_container_width=True,
        )

        st.divider()

        # System Health Status
        health = check_agent_health(agent_url)
        badge_color = "#00ff66" if health["status"] == "ok" else "#ffaa00" if health["status"] == "degraded" else "#ff4444"
        st.markdown(
            f'<div style="font-size: 0.8rem; color: #888;">Agent Connection: '
            f'<span style="color: {badge_color}; font-weight: bold;">● {health["label"]}</span></div>',
            unsafe_allow_html=True,
        )

    return current_id
