"""AWS event stream parser for Kiro API responses.

Ported from kiro-gateway parsers.py. Handles the binary AWS SSE format
that Kiro uses (not standard text SSE).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger("unified.kiro.parsers")


# ---------------------------------------------------------------------------
# Bracket-style tool call parsing
# ---------------------------------------------------------------------------

def find_matching_brace(text: str, start: int) -> int:
    """Find the matching closing brace for an opening brace at position start."""
    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_bracket_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Extract [Called func_name with args: {...}] patterns from text."""
    pattern = r'\[Called\s+(\w+)\s+with\s+args:\s*'
    results: List[Dict[str, Any]] = []

    for m in re.finditer(pattern, text):
        func_name = m.group(1)
        brace_start = text.find('{', m.end() - 1)
        if brace_start == -1:
            continue
        brace_end = find_matching_brace(text, brace_start)
        if brace_end == -1:
            continue
        json_str = text[brace_start:brace_end + 1]
        try:
            args = json.loads(json_str)
        except json.JSONDecodeError:
            continue
        results.append({
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": func_name,
                "arguments": json.dumps(args),
            },
        })
    return results


def deduplicate_tool_calls(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate tool calls by id (prefer non-empty args) then by name+args."""
    if not tool_calls:
        return []

    # First pass: dedup by id
    by_id: Dict[str, Dict[str, Any]] = {}
    for tc in tool_calls:
        tc_id = tc.get("id", "")
        if tc_id in by_id:
            # Prefer the one with non-empty arguments
            existing_args = by_id[tc_id].get("function", {}).get("arguments", "")
            new_args = tc.get("function", {}).get("arguments", "")
            if not existing_args and new_args:
                by_id[tc_id] = tc
        else:
            by_id[tc_id] = tc

    # Second pass: dedup by name+args
    seen: set = set()
    result: List[Dict[str, Any]] = []
    for tc in by_id.values():
        func = tc.get("function", {})
        key = (func.get("name", ""), func.get("arguments", ""))
        if key not in seen:
            seen.add(key)
            result.append(tc)

    return result


# ---------------------------------------------------------------------------
# AWS Event Stream Binary Parser
# ---------------------------------------------------------------------------

class AwsEventStreamParser:
    """Parse binary AWS event stream format from Kiro API.

    The Kiro API returns responses in AWS event stream format (binary),
    not standard text SSE. This parser extracts content, tool calls,
    usage, and context_usage events from the binary stream.
    """

    # Regex patterns for extracting data from binary event payloads
    EVENT_PATTERNS = {
        "content": re.compile(
            rb'"assistantResponseEvent"\s*:\s*\{[^}]*"content"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
        ),
        "tool_start": re.compile(
            rb'"toolUse"\s*:\s*\{[^}]*"toolUseId"\s*:\s*"([^"]*)"[^}]*"name"\s*:\s*"([^"]*)"'
        ),
        "tool_input": re.compile(
            rb'"toolUse"\s*:\s*\{[^}]*"input"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
        ),
        "tool_stop": re.compile(
            rb'"toolUse"\s*:\s*\{[^}]*"stopReason"\s*:\s*"([^"]*)"'
        ),
        "followup": re.compile(
            rb'"followupPrompt"\s*:\s*\{[^}]*"content"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
        ),
        "usage": re.compile(
            rb'"supplementaryWebLinks"\s*:\s*\[([^\]]*)\]'
        ),
        "context_usage": re.compile(
            rb'"contextUsagePercentage"\s*:\s*(\d+(?:\.\d+)?)'
        ),
    }

    def __init__(self) -> None:
        self._buffer = b""
        self._tool_calls: List[Dict[str, Any]] = []
        self._current_tool: Optional[Dict[str, Any]] = None
        self._current_tool_input_parts: List[str] = []
        self._seen_content: set = set()  # dedup content chunks

    def feed(self, chunk: bytes) -> List[Dict[str, str | Any]]:
        """Feed a chunk of bytes and return parsed events."""
        self._buffer += chunk
        events: List[Dict[str, str | Any]] = []

        # Process complete messages from buffer
        while len(self._buffer) >= 12:
            # AWS event stream: first 4 bytes = total length (big-endian)
            total_len = int.from_bytes(self._buffer[:4], "big")
            if total_len < 12 or total_len > 1_000_000:
                # Invalid frame — skip 1 byte and retry
                self._buffer = self._buffer[1:]
                continue
            if len(self._buffer) < total_len:
                break  # incomplete message, wait for more data

            message = self._buffer[:total_len]
            self._buffer = self._buffer[total_len:]

            # Extract events from this message
            msg_events = self._parse_message(message)
            events.extend(msg_events)

        return events

    def _parse_message(self, message: bytes) -> List[Dict[str, str | Any]]:
        """Parse a single AWS event stream message.

        AWS event stream binary format:
        - 4 bytes: total length (big-endian)
        - 4 bytes: headers length (big-endian)
        - 4 bytes: prelude CRC
        - N bytes: headers (binary key-value pairs)
        - M bytes: payload (JSON)
        - 4 bytes: message CRC
        """
        events: List[Dict[str, str | Any]] = []

        if len(message) < 16:
            return events

        total_len = int.from_bytes(message[:4], "big")
        headers_len = int.from_bytes(message[4:8], "big")

        headers_start = 12
        headers_end = 12 + headers_len
        payload_start = headers_end
        payload_end = total_len - 4  # minus message CRC

        if payload_end <= payload_start or payload_end > len(message):
            return events

        # Extract event type from binary headers
        event_type = self._extract_header(message[headers_start:headers_end], b":event-type")

        # Extract JSON payload
        payload_bytes = message[payload_start:payload_end]

        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, ValueError):
            # Not JSON — try regex fallback on raw message for edge cases
            return self._parse_message_regex_fallback(message)

        # Route based on event type
        if event_type == "assistantResponseEvent":
            content = payload.get("content", "")
            if content:
                event = self._process_content_event(content.encode("utf-8"))
                if event:
                    events.append(event)

        elif event_type == "toolUseEvent":
            tool_use = payload.get("toolUse", {})
            if "toolUseId" in tool_use and "name" in tool_use:
                # Tool start
                self._process_tool_start_event(tool_use["toolUseId"], tool_use["name"])
            elif "input" in tool_use:
                # Tool input delta
                self._process_tool_input_event(tool_use["input"])
            elif "stopReason" in tool_use:
                # Tool stop
                self._finalize_tool_call()

        elif event_type == "supplementaryWebLinksEvent":
            events.append({"type": "usage", "data": {}})

        # Check for context usage in any event payload
        if "contextUsagePercentage" in payload:
            try:
                pct = float(payload["contextUsagePercentage"])
                events.append({"type": "context_usage", "data": pct})
            except (ValueError, TypeError):
                pass

        # Also check nested structures for content/usage
        if not events and not event_type:
            # Fallback: try regex on raw bytes for unknown formats
            return self._parse_message_regex_fallback(message)

        return events

    @staticmethod
    def _extract_header(headers_bytes: bytes, header_name: bytes) -> str:
        """Extract a string header value from AWS event stream binary headers."""
        pos = 0
        while pos < len(headers_bytes):
            if pos >= len(headers_bytes):
                break
            name_len = headers_bytes[pos]
            pos += 1
            if pos + name_len > len(headers_bytes):
                break
            name = headers_bytes[pos:pos + name_len]
            pos += name_len
            if pos >= len(headers_bytes):
                break
            value_type = headers_bytes[pos]
            pos += 1
            if value_type == 7:  # string type
                if pos + 2 > len(headers_bytes):
                    break
                value_len = int.from_bytes(headers_bytes[pos:pos + 2], "big")
                pos += 2
                if pos + value_len > len(headers_bytes):
                    break
                value = headers_bytes[pos:pos + value_len]
                pos += value_len
                if name == header_name:
                    return value.decode("utf-8", errors="replace")
            else:
                # Unknown type — can't parse further
                break
        return ""

    def _parse_message_regex_fallback(self, message: bytes) -> List[Dict[str, str | Any]]:
        """Fallback: use regex patterns on raw bytes for unknown message formats."""
        events: List[Dict[str, str | Any]] = []

        m = self.EVENT_PATTERNS["content"].search(message)
        if m:
            event = self._process_content_event(m.group(1))
            if event:
                events.append(event)

        m = self.EVENT_PATTERNS["tool_start"].search(message)
        if m:
            self._process_tool_start_event(m.group(1).decode("utf-8", errors="replace"),
                                           m.group(2).decode("utf-8", errors="replace"))

        m = self.EVENT_PATTERNS["tool_input"].search(message)
        if m:
            self._process_tool_input_event(m.group(1).decode("utf-8", errors="replace"))

        m = self.EVENT_PATTERNS["tool_stop"].search(message)
        if m:
            self._finalize_tool_call()

        m = self.EVENT_PATTERNS["context_usage"].search(message)
        if m:
            try:
                pct = float(m.group(1).decode("utf-8"))
                events.append({"type": "context_usage", "data": pct})
            except (ValueError, UnicodeDecodeError):
                pass

        m = self.EVENT_PATTERNS["usage"].search(message)
        if m:
            events.append({"type": "usage", "data": {}})

        return events

    def _process_content_event(self, raw: bytes) -> Optional[Dict[str, str]]:
        """Process a content event, with deduplication."""
        try:
            text = raw.decode("utf-8", errors="replace")
            # Unescape JSON string escapes
            text = text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
        except Exception:
            return None

        # Dedup: skip if we've seen this exact content recently
        content_hash = hash(text)
        if content_hash in self._seen_content:
            return None
        self._seen_content.add(content_hash)

        # Limit dedup set size
        if len(self._seen_content) > 1000:
            self._seen_content.clear()

        return {"type": "content", "data": text}

    def _process_tool_start_event(self, tool_use_id: str, name: str) -> None:
        """Start accumulating a new tool call."""
        # Finalize any in-progress tool
        if self._current_tool:
            self._finalize_tool_call()

        self._current_tool = {
            "id": tool_use_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": "",
            },
        }
        self._current_tool_input_parts = []

    def _process_tool_input_event(self, input_text: str) -> None:
        """Accumulate tool input text."""
        if self._current_tool is not None:
            self._current_tool_input_parts.append(input_text)

    def _finalize_tool_call(self) -> None:
        """Finalize the current tool call and add to list."""
        if self._current_tool is None:
            return

        raw_input = "".join(self._current_tool_input_parts)

        # Try to parse as JSON
        try:
            parsed = json.loads(raw_input)
            self._current_tool["function"]["arguments"] = json.dumps(parsed)
        except json.JSONDecodeError:
            # Check for truncation
            diagnosis = self._diagnose_json_truncation(raw_input)
            if diagnosis:
                log.warning("Tool call JSON truncated: %s", diagnosis)
            self._current_tool["function"]["arguments"] = raw_input

        self._tool_calls.append(self._current_tool)
        self._current_tool = None
        self._current_tool_input_parts = []

    def _diagnose_json_truncation(self, text: str) -> Optional[str]:
        """Detect unbalanced braces/brackets/quotes in JSON text."""
        if not text:
            return None

        brace_depth = 0
        bracket_depth = 0
        in_string = False
        escape_next = False

        for ch in text:
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
            elif ch == '[':
                bracket_depth += 1
            elif ch == ']':
                bracket_depth -= 1

        issues = []
        if brace_depth > 0:
            issues.append(f"{brace_depth} unclosed braces")
        if bracket_depth > 0:
            issues.append(f"{bracket_depth} unclosed brackets")
        if in_string:
            issues.append("unclosed string")

        return ", ".join(issues) if issues else None

    def get_tool_calls(self) -> List[Dict[str, Any]]:
        """Get all accumulated tool calls."""
        # Finalize any in-progress tool
        if self._current_tool:
            self._finalize_tool_call()
        return self._tool_calls

    def reset(self) -> None:
        """Reset parser state."""
        self._buffer = b""
        self._tool_calls = []
        self._current_tool = None
        self._current_tool_input_parts = []
        self._seen_content.clear()
