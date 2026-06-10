"""Prompt contract for desktop Chrome integration checks."""

from __future__ import annotations

DESKTOP_CHROME_RD_PROMPT = """
You are operating the user's desktop Google Chrome app for Quant Forge local RD checks.

Follow this sequence:
1. Connect to or launch the desktop app named "Google Chrome".
2. Before using screenshot/window-state tools, verify the front tab with AppleScript:
   tell application "Google Chrome" to get URL of active tab of front window
3. Navigate the desktop Chrome tab to the local Quant Forge URL.
4. Prefer page-context JavaScript/fetch for deterministic local web actions.
5. Only use screenshot/click/type when page-context JavaScript is unavailable.
6. If Computer Use get_app_state returns cgWindowNotFound while AppleScript can read the
   Chrome URL, continue with desktop Chrome AppleScript/DOM/fetch and report fallback_used=true.

Every integration step should report one JSON object:
{
  "step": "short step name",
  "method": "computer_use|applescript|dom_fetch|screenshot_click",
  "target_url": "http://127.0.0.1:8765/",
  "success": true,
  "evidence": "short observable result",
  "fallback_used": false
}
""".strip()


def desktop_chrome_rd_prompt() -> str:
    return DESKTOP_CHROME_RD_PROMPT
