import shlex
import subprocess
import difflib
import logging
import json
import os
import sys
from typing import Callable, Dict, List, Optional, Any
from core.brain import get_provider
from core.logger import ErrorLogger

logger = logging.getLogger("jade.command_handler")

class CommandError(Exception):
    pass

class CommandHandler:
    def __init__(self, shell_timeout: int = 180):
        self._registry: Dict[str, Dict[str, Any]] = {}
        self.shell_timeout = shell_timeout

    def register(self, name: str, func: Callable[..., Any], aliases: Optional[List[str]] = None, help: str = ""):
        entry = {"func": func, "aliases": set(aliases or []), "help": help}
        name_slash = name if name.startswith("/") else "/" + name
        self._registry[name_slash] = entry
        for a in aliases or []:
            a_slash = a if a.startswith("/") else "/" + a
            self._registry[a_slash] = entry

    def list_commands(self):
        seen = set()
        out = []
        for name, meta in self._registry.items():
            if id(meta["func"]) in seen: continue
            seen.add(id(meta["func"]))
            out.append((name, meta["help"]))
        return out

    def _tokenize(self, text: str) -> List[str]:
        try:
            return shlex.split(text)
        except Exception as e:
            ErrorLogger.log_error(e, context="command_handler._tokenize")
            return text.split()

    def _match_known(self, tokens: List[str]):
        if not tokens: return None, None
        candidate = tokens[0]
        
        # Check for exact match (with or without slash)
        candidate_slash = candidate if candidate.startswith("/") else "/" + candidate
        if candidate_slash in self._registry:
            return candidate_slash, tokens[1:]
            
        # Only allow fuzzy matching if it explicitly starts with '/'
        # This prevents natural language from being misidentified as a command.
        if not candidate.startswith("/"):
            return None, None
        
        keys = list(self._registry.keys())
        close = difflib.get_close_matches(candidate, keys, n=1, cutoff=0.7)
        if close:
            return close[0], tokens[1:]
        return None, None

    def suggest(self, text: str, n: int = 3, cutoff: float = 0.55) -> List[str]:
        """Closest registered commands for a mistyped slash command.

        Pure difflib — no LLM call. Returns up to n command names the user
        can actually type.
        """
        candidate = text.strip().split()[0] if text.strip() else ""
        if not candidate.startswith("/"):
            candidate = "/" + candidate
        keys = list(self._registry.keys())
        close = difflib.get_close_matches(candidate, keys, n=n * 2, cutoff=cutoff)
        seen: set = set()
        out: List[str] = []
        for k in close:
            if k not in seen:
                seen.add(k)
                out.append(k)
            if len(out) >= n:
                break
        return out

    def resolve(self, text: str):
        """Public wrapper around _match_known: returns (command, args) or
        (None, None). Pure local matching — no LLM call."""
        return self._match_known(self._tokenize(text))

    def _call_llm_parse(self, text: str) -> Dict[str, Any]:
        """
        Deterministic intent parser using strict JSON schema, few-shot examples, and UI hints.
        """
        system_instruction = """
You are an intent parser for a command-line assistant. Your job is to parse a single user utterance into a single strict JSON object only — no surrounding text, no markdown, no explanation. The JSON MUST exactly follow the schema below. If you cannot confidently parse, return the "noop" fallback form. Always use lowercase canonical command names.

JSON schema:
{
  "type": "object",
  "properties": {
    "type": { "type": "string", "enum": ["internal", "shell", "help", "noop"] },
    "target": { "type": "string" },
    "args": { "type": "array", "items": { "type": "string" } },
    "confirm": { "type": "boolean" },
    "ui": {
      "type": ["object","null"],
      "properties": {
        "title": { "type": "string" },
        "subtitle": { "type": "string" },
        "markdown": { "type": "string" },
        "buttons": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "label": { "type": "string" },
              "action": { "type": "string", "enum": ["confirm","cancel","help","run","open"] },
              "primary": { "type": "boolean" }
            },
            "required": ["label","action"],
            "additionalProperties": false
          }
        }
      },
      "additionalProperties": false
    }
  },
  "required": ["type","target","args","confirm"],
  "additionalProperties": false
}

Notes about "ui":
- "ui" is OPTIONAL. When present it provides Gemini-style UI hints for the frontend.
- The top-level fields (type/target/args/confirm) remain authoritative.

Interpretation rules:
- type: "internal" = call registered command; "shell" = run shell; "help" = return listing; "noop" = no-op.
- target: Canonical command name for "internal"; exact string for "shell".
- confirm: true for destructive operations.

Few-shot examples:
User: "scan /etd drive  for useful code to use in n4v3r41n program"
Assistant: {"type":"internal","target":"locate","args":["/etd","n4v3r41n"],"confirm":false,"ui":{"title":"Scan /etd for code","subtitle":"Suggested search for n4v3r41n","markdown":"Found likely candidates: repository files.","buttons":[{"label":"Run scan","action":"run","primary":true},{"label":"Show help","action":"help"}]}}

User: "fix broken code in n4v3r41n"
Assistant: {"type":"internal","target":"fix","args":["n4v3r41n"],"confirm":false,"ui":{"title":"Fix n4v3r41n","subtitle":"Autonomous repair cycle","markdown":"Analyzing project directory for bugs.","buttons":[{"label":"Confirm Fix","action":"confirm","primary":true}]}}

User: "run git status"
Assistant: {"type":"shell","target":"git status","args":[],"confirm":false,"ui":{"title":"Run git status","subtitle":"Read-only status check","buttons":[{"label":"Run","action":"run","primary":true}]}}

User: "what can you do?"
Assistant: {"type":"help","target":"","args":[],"confirm":false,"ui":{"title":"Help: Commands","subtitle":"List available functions","buttons":[{"label":"Show commands","action":"help","primary":true}]}}
"""
        provider = get_provider()
        response = provider.ask(f"{system_instruction}\n\nUser: \"{text}\"\nAssistant:", options={"temperature": 0, "top_p": 1})
        
        try:
            res_clean = response.strip()
            if "```json" in res_clean:
                res_clean = res_clean.split("```json")[1].split("```")[0]
            elif "{" in res_clean:
                res_clean = res_clean[res_clean.find("{"):res_clean.rfind("}")+1]
            
            obj = json.loads(res_clean)
            if all(k in obj for k in ["type", "target", "args", "confirm"]):
                return obj
        except Exception as e:
            ErrorLogger.log_error(e, context="command_handler._call_llm_parse")

        return {"type": "noop", "target": "", "args": [], "confirm": False, "ui": None}

    @staticmethod
    def _join_args(args: Any) -> str:
        """Coerce an LLM-returned args payload to a string. The model is
        contractually supposed to return a list of strings, but under stress
        it returns None, scalars, or mixed lists — never let that crash routing."""
        if args is None:
            return ""
        if isinstance(args, str):
            return args
        try:
            return " ".join(str(a) for a in args)
        except TypeError:
            return str(args)

    def handle(self, text: str) -> Dict[str, Any]:
        if not isinstance(text, str):
            return {"ok": False, "error": "invalid input: expected text"}
        text = text.strip()
        if not text: return {"ok": False, "error": "empty"}

        tokens = self._tokenize(text)
        matched, args = self._match_known(tokens)
        if matched:
            return {"ok": True, "type": "internal", "command": matched, "args": " ".join(args), "ui": None}

        parsed = self._call_llm_parse(text)
        t = parsed.get("type")
        
        if t == "internal":
            target = parsed.get("target") or ""
            if not isinstance(target, str):
                target = str(target)
            cmd = target if target.startswith("/") else "/" + target
            return {"ok": True, "type": "internal", "command": cmd, "args": self._join_args(parsed.get("args")), "ui": parsed.get("ui")}
        elif t == "shell":
            return {"ok": True, "type": "shell", "command": parsed.get("target"), "confirm": parsed.get("confirm", True), "ui": parsed.get("ui")}
        elif t == "help":
            return {"ok": True, "type": "internal", "command": "/help", "args": "", "ui": parsed.get("ui")}
        
        return {"ok": True, "type": "chat", "args": text, "ui": parsed.get("ui")}
