"""Incident context drawer for pasting JES2/3 spool, SYSLOG, or abend dumps."""

from __future__ import annotations

import streamlit as st


def render_incident_drawer() -> tuple[str | None, str | None, str | None]:
    """Render a collapsible drawer for attaching mainframe incident context.

    Returns:
        tuple of (splunk_context, product_filter, version_filter)
    """
    with st.expander("📋 Incident Context & Subsystem Filters (JES Spool / Abend Dump)", expanded=False):
        st.caption(
            "Paste raw job logs, abend dumps (S0C4, S0C1), or SYSLOG excerpts here. "
            "Context is fed into the reasoning LLM prompt alongside retrieved manual pages."
        )

        splunk_context = st.text_area(
            "JES Spool / SYSLOG / Abend Dump",
            height=130,
            placeholder=(
                "//STEP1    EXEC PGM=IEFBR14\n"
                "IEF450I JOB12345 STEP1 - ABEND=S0C4 U0000 REASON=00000004\n"
                "PSW AT TIME OF INTERRUPT: 078D0000 8000A124"
            ),
            key="drawer_splunk_context",
        )

        col1, col2 = st.columns(2)
        with col1:
            product = st.selectbox(
                "Filter Subsystem (Optional)",
                options=["", "z/OS", "CICS", "Db2", "IMS", "RACF", "JES2", "z/VM"],
                index=0,
                key="drawer_product",
            )
        with col2:
            version = st.text_input(
                "Subsystem Version (Optional)",
                placeholder="e.g. 2.5, 3.1, 13",
                key="drawer_version",
            )

        ctx = splunk_context.strip() if splunk_context.strip() else None
        prod = product.strip() if product.strip() else None
        ver = version.strip() if version.strip() else None

        return ctx, prod, ver
