"""Themes and CSS styling for Mainframe Operations Copilot UI.

Provides Modern Dark mode and authentic IBM 3270 Green Phosphor Terminal styles.
"""

from __future__ import annotations

MODERN_DARK_CSS = """
<style>
/* Modern Dark Theme */
.stApp {
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
.stSidebar {
    background-color: #161b22;
    border-right: 1px solid #30363d;
}
.chat-message {
    padding: 1rem;
    border-radius: 6px;
    margin-bottom: 1rem;
    border: 1px solid #30363d;
}
.citation-card {
    background-color: #161b22;
    border-left: 3px solid #58a6ff;
    padding: 0.6rem 0.8rem;
    margin: 0.4rem 0;
    border-radius: 4px;
    font-size: 0.85rem;
}
.citation-badge {
    background-color: #1f6feb;
    color: #ffffff;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-right: 6px;
}
.incident-box {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 1rem;
}
</style>
"""

IBM_3270_CSS = """
<style>
/* IBM 3270 Green Phosphor Terminal Theme */
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');

.stApp {
    background-color: #000000 !important;
    color: #00ff66 !important;
    font-family: 'Share Tech Mono', 'Courier New', Courier, monospace !important;
    text-shadow: 0 0 2px #00ff66, 0 0 5px rgba(0, 255, 102, 0.4);
}
.stSidebar {
    background-color: #050d05 !important;
    border-right: 2px solid #00aa44 !important;
}
h1, h2, h3, h4, h5, h6, p, span, label, .stMarkdown {
    color: #00ff66 !important;
    font-family: 'Share Tech Mono', 'Courier New', Courier, monospace !important;
}
.stTextInput > div > div > input, .stTextArea > div > div > textarea {
    background-color: #001100 !important;
    color: #00ff66 !important;
    border: 1px solid #00aa44 !important;
    font-family: 'Share Tech Mono', 'Courier New', Courier, monospace !important;
}
.stButton > button {
    background-color: #002200 !important;
    color: #00ff66 !important;
    border: 1px solid #00ff66 !important;
    font-family: 'Share Tech Mono', 'Courier New', Courier, monospace !important;
    text-transform: uppercase;
    letter-spacing: 1px;
}
.stButton > button:hover {
    background-color: #00ff66 !important;
    color: #000000 !important;
}
.citation-card {
    background-color: #001a00;
    border: 1px solid #00aa44;
    border-left: 4px solid #00ff66;
    padding: 0.6rem 0.8rem;
    margin: 0.4rem 0;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.85rem;
    color: #00ff66;
}
.citation-badge {
    background-color: #00aa44;
    color: #000000;
    padding: 2px 6px;
    font-weight: bold;
    font-size: 0.75rem;
    margin-right: 6px;
}
.incident-box {
    background-color: #001100;
    border: 1px dashed #00aa44;
    padding: 1rem;
    margin-bottom: 1rem;
}
pre, code {
    background-color: #001100 !important;
    color: #33ff88 !important;
    border: 1px solid #006622 !important;
}
</style>
"""


def get_theme_css(theme: str) -> str:
    """Return the CSS block for the specified UI theme."""
    if theme.lower().startswith("ibm") or "3270" in theme.lower():
        return IBM_3270_CSS
    return MODERN_DARK_CSS
