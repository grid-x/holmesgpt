"""
Prefix-based command validation for the bash toolset.

This module provides validation logic for bash commands using prefix matching
against allow/deny lists, with support for composed commands (pipes, &&, etc.).
"""

import logging
import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from holmes.common.env_vars import HOLMES_TOOL_RESULT_STORAGE_PATH, load_bool

from holmes.plugins.toolsets.bash.common.config import (
    HARDCODED_BLOCKS,
    BashExecutorConfig,
)
from holmes.plugins.toolsets.bash.common.default_lists import (
    CORE_ALLOW_LIST,
    DEFAULT_DENY_LIST,
    EXTENDED_ALLOW_LIST,
)
from holmes.plugins.toolsets.bash.argv_utils import is_benign_redirect_target
from holmes.plugins.toolsets.bash.command_arg_rules import (
    dangerous_argv_reason,
    is_argv_checked_command,
)
from holmes.plugins.toolsets.bash.shell_parser import (
    ParsedCommand,
    ShellParseError,
    parse_command,
    scan_for_deny_checks,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument-level (argv) security checks.
#
# Prefix matching validates only a command's *name*. Some allow-listed commands
# accept arguments (or shell redirections) that turn a read-only tool into
# arbitrary code execution, file writes, or deletion. These checks inspect the
# parsed argv/redirections and DENY those primitives regardless of allow-list
# membership or prior approval, so they are never auto-executed. (This operates
# on the parsed command; commands shell_parser cannot parse are routed to
# approval by validate_command and so are never auto-executed either.)
#
# Scope note: `tar`/`zcat`/`zgrep`/`gzip` are intentionally NOT in the builtin
# allow lists (see default_lists.py); any use of them already requires approval,
# so they need no argv rule here.
#
# The per-command rules (find/sort/uniq) live in command_arg_rules.py and the
# generic argv/target helpers in argv_utils.py; this module turns a reported
# reason into a DENY/APPROVAL verdict.
# ---------------------------------------------------------------------------


class ValidationStatus(Enum):
    """Result status for command validation."""

    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


class DenyReason(Enum):
    """Reason why a command was denied."""

    HARDCODED_BLOCK = "hardcoded_block"
    DENY_LIST = "deny_list"
    PREFIX_NOT_IN_COMMAND = "fabricated_prefix"
    DANGEROUS_ARGUMENT = "dangerous_argument"


@dataclass
class ValidationResult:
    """Result of command validation."""

    status: ValidationStatus
    deny_reason: Optional[DenyReason] = None
    message: Optional[str] = None
    # Prefixes that need approval (for APPROVAL_REQUIRED status)
    prefixes_needing_approval: Optional[List[str]] = None


def get_effective_lists(config: BashExecutorConfig) -> Tuple[List[str], List[str]]:
    """
    Get the effective allow and deny lists based on configuration.

    builtin_allowlist controls which builtin list is merged with user-provided entries:
    - "core": kubectl read-only, jq, grep, text processing, system info
    - "extended": core + filesystem commands (cat, find, ls, base64)
    - "none": only user-provided allow/deny entries

    Returns copies to prevent mutation of the shared config.

    Returns:
        Tuple of (allow_list, deny_list) - always returns copies, never references
    """
    if config.builtin_allowlist == "extended":
        builtin = EXTENDED_ALLOW_LIST
    elif config.builtin_allowlist == "core":
        builtin = CORE_ALLOW_LIST
    else:
        builtin = []

    # Auto-allow read-only commands for the tool result storage directory so the
    # LLM can access saved large tool results without approval prompts.
    tool_result_prefixes: List[str] = []
    if load_bool("HOLMES_TOOL_RESULT_STORAGE_ENABLED", True):
        storage_path = HOLMES_TOOL_RESULT_STORAGE_PATH
        tool_result_prefixes = [
            f"cat {storage_path}",
            f"head {storage_path}",
            f"tail {storage_path}",
            f"wc {storage_path}",
            f"jq {storage_path}",
        ]

    allow_list = sorted(set(builtin + config.allow + tool_result_prefixes))
    deny_list = sorted(set(DEFAULT_DENY_LIST + config.deny))

    return allow_list, deny_list


def parse_command_segments(command: str) -> Tuple[List[str], bool]:
    """
    Parse a command into segments separated by |, &&, ||, ;, &.

    Returns:
        Tuple of (segments, contains_compound_command):
        - segments: List of command segments extracted from the command
        - contains_compound_command: True if compound statements (for, while, if, etc.) were detected

    Raises:
        ShellParseError: If the command cannot be parsed (invalid or unsupported syntax)
    """
    parsed = parse_command(command)
    return (parsed.segments, parsed.contains_compound_command)


def _unsafe_args_approval_mode() -> bool:
    # Unknown/empty values fail safe to the strict "deny" behaviour.
    return os.environ.get("HOLMES_BASH_UNSAFE_ARGS_MODE", "deny").strip().lower() == "approval"


def _unsafe_arg_result(reason: str, approval_mode: bool) -> ValidationResult:
    """Block an exec/write vector: DENIED by default, or (in approval mode)
    APPROVAL_REQUIRED so a human can allow it. Either way it never auto-executes."""
    if approval_mode:
        return ValidationResult(
            status=ValidationStatus.APPROVAL_REQUIRED,
            message=(
                f"Command requires approval: {reason}. The bash toolset is "
                "read-only, so this is not auto-executed."
            ),
            prefixes_needing_approval=[],
        )
    return ValidationResult(
        status=ValidationStatus.DENIED,
        deny_reason=DenyReason.DANGEROUS_ARGUMENT,
        message=(
            f"Command blocked for security reasons: {reason}. The bash toolset "
            "is read-only; this is not auto-executed."
        ),
    )


def check_dangerous_argv(parsed: ParsedCommand) -> Optional[ValidationResult]:
    """Argv-level and redirection security checks that prefix matching cannot see.

    Returns:
        - DENIED if any command segment uses a dangerous argument primitive or
          writes to a real file via output redirection (checked first, so a hard
          deny is never downgraded to approval);
        - APPROVAL_REQUIRED if an argv-checked command (find/sort/uniq) builds an
          argument via shell expansion, whose runtime value we cannot inspect
          statically and which could smuggle a blocked primitive;
        - None otherwise.

    This inspects the parsed command, so it applies to commands shell_parser can
    parse. Commands it cannot parse never reach here — validate_command routes
    them to APPROVAL_REQUIRED (human in the loop), so they are never auto-executed.

    How the exec/write vectors are handled is set by HOLMES_BASH_UNSAFE_ARGS_MODE:
      - "deny" (default): block them outright (they are never auto-executed and
        cannot be approved);
      - "approval": still not auto-executed, but a human may approve a genuinely
        read-only use (e.g. `find … -exec grep …`).
    Any other value falls back to "deny". This does NOT auto-run anything either
    way — it only chooses between blocking and prompting a human.
    """
    approval_mode = _unsafe_args_approval_mode()

    # DENY (or, in approval mode, gate) checks first, across ALL segments, so a
    # hard block is never downgraded by an earlier segment that merely contains a
    # shell expansion.
    for argv in parsed.command_argvs:
        reason = dangerous_argv_reason(argv)
        if reason:
            return _unsafe_arg_result(reason, approval_mode)

    if parsed.write_redirect_targets:
        target = parsed.write_redirect_targets[0]
        return _unsafe_arg_result(
            f"output redirection to '{target}' writes to the filesystem",
            approval_mode,
        )

    # No hard deny. A runtime expansion ($(...), `...`, $VAR/${VAR}, <(...)) in an
    # argv-checked command's arguments can expand into a blocked primitive that the
    # static checks above cannot see, so require explicit approval rather than
    # auto-allowing it.
    for argv, arg_is_dynamic in zip(
        parsed.command_argvs, parsed.command_arg_dynamic, strict=True
    ):
        if arg_is_dynamic and is_argv_checked_command(os.path.basename(argv[0])):
            return ValidationResult(
                status=ValidationStatus.APPROVAL_REQUIRED,
                message=(
                    f"'{os.path.basename(argv[0])}' builds an argument via shell "
                    "expansion, which cannot be verified as read-only and requires "
                    "approval."
                ),
                prefixes_needing_approval=[],
            )

    return None


def check_hardcoded_blocks(segment: str) -> Optional[str]:
    """
    Check if segment matches any hardcoded block patterns.
    Uses same matching logic as deny list for consistency.

    Args:
        segment: A single command segment (already parsed)

    Returns:
        The matched block pattern if found, None otherwise
    """
    segment_lower = segment.lower()
    for block in HARDCODED_BLOCKS:
        if match_prefix_for_deny(segment_lower, block):
            return block

    return None


def check_blocked_in_raw_command(command: str, blocked_list: List[str]) -> Optional[str]:
    """
    Check for blocked patterns anywhere in a raw command string using word boundaries.

    This is the fallback safety check for when shell_parser can't parse the command.
    It scans the entire raw command for any pattern from the given list.

    Args:
        command: The full raw command string (may contain compound statements, subshells, etc.)
        blocked_list: List of command patterns to check for (e.g. HARDCODED_BLOCKS or deny_list)

    Returns:
        The matched pattern if found, None otherwise
    """
    command_lower = command.lower()
    for pattern in blocked_list:
        if re.search(rf"\b{re.escape(pattern.lower())}\b", command_lower):
            return pattern
    return None


# Words that start a command without being its name (`do find ...`, `! find ...`).
_RAW_LEADING_KEYWORDS = frozenset(
    {"!", "{", "}", "do", "then", "else", "elif", "if", "while", "until", "time", "coproc"}
)
_RAW_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RAW_OPERATOR_CHARS = "();<>|&"
# Escaped operator characters are hidden from shlex (which can't tell `\;` from
# `;`) as private-use characters, and restored afterwards.
_RAW_ESCAPED = {c: chr(0xE000 + ord(c)) for c in _RAW_OPERATOR_CHARS}
_RAW_UNESCAPE = str.maketrans({v: k for k, v in _RAW_ESCAPED.items()})
_RAW_HEREDOC = re.compile(r"""(?<!<)<<(-?)\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2""")


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies, which are data (`key: >`, HTML), not commands.
    Bodies of unquoted heredocs that contain a substitution are kept: bash runs
    those substitutions."""
    lines = command.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        # a `<<` inside quotes is not a heredoc
        heredocs = [m.groups() for m in _RAW_HEREDOC.finditer(lines[i])
                    if lines[i][: m.start()].count("'") % 2 == 0 and lines[i][: m.start()].count('"') % 2 == 0]
        i += 1
        for strip_tabs, quote, delimiter in heredocs:
            end = i
            while end < len(lines) and (lines[end].lstrip("\t") if strip_tabs else lines[end]) != delimiter:
                end += 1
            body = lines[i:end]
            if not quote and any(re.search(r"`|\$\(", line) for line in body):
                out += body
            i = end + 1 if end < len(lines) else end
            if end < len(lines):
                out.append(lines[end])
    return "\n".join(out)


def _raw_tokens(command: str) -> List[str]:
    """Best-effort shell tokenization for commands shell_parser can't parse.
    Lines that can't be tokenized (e.g. unbalanced quotes) are skipped. Escaped
    operator characters stay hidden in the tokens (see _RAW_ESCAPED)."""
    hidden = re.sub(r"\\([();<>|&])", lambda m: _RAW_ESCAPED[m.group(1)], _strip_heredoc_bodies(command))
    tokens: List[str] = []
    for chunk in [hidden] + hidden.splitlines():
        lexer = shlex.shlex(chunk, posix=True, punctuation_chars=_RAW_OPERATOR_CHARS)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            chunk_tokens = list(lexer)
        except ValueError:
            continue
        if chunk is hidden:
            tokens = chunk_tokens
            break
        tokens += chunk_tokens + [";"]
    return tokens


def _raw_argvs(command: str) -> Tuple[List[List[str]], List[str]]:
    """(argvs, write redirect targets) from a shlex tokenization of the command."""
    tokens = _raw_tokens(command)
    argvs: List[List[str]] = [[]]
    # (argv, mode, arith depth) enclosing an open $( / <( / >( / $(( / ((
    parents: List[Tuple[List[str], str, int]] = []
    targets: List[str] = []
    # "cmd": tokens are commands; "test": inside `[[ ]]`, "arith": inside
    # `$(( ))` / `(( ))`. In the last two `<` and `>` compare, not redirect.
    mode, depth = "cmd", 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        argv = argvs[-1]
        is_operator = bool(tok) and all(c in _RAW_OPERATOR_CHARS for c in tok)
        if tok.endswith("$") and nxt.startswith("((") or mode == "cmd" and not argv and tok.startswith("(("):
            # arithmetic: `>` and `<` inside compare
            opener = nxt if tok.endswith("$") else tok
            if tok.endswith("$"):
                argv.append(tok.rstrip("$") + "$((...))")
            parents.append((argv, mode, depth))
            mode, depth = "arith", opener.count("(") - opener.count(")")
            argvs.append([])
            i += 2 if tok.endswith("$") else 1
            continue
        if tok.endswith("$") and nxt.startswith("(") or tok in ("<(", ">("):
            # command / process substitution: a word here, a command inside
            argv.append(tok.rstrip("$") + "$(...)")
            parents.append((argv, mode, depth))
            mode, depth = "cmd", 0
            argvs.append([])
            i += 1 if tok in ("<(", ">(") else 2
            continue
        if mode == "arith":
            if is_operator:
                depth += tok.count("(") - tok.count(")")
                if depth <= 0 and parents:
                    parent, mode, depth = parents.pop()
                    argvs.append(parent)
            i += 1
            continue
        if mode == "test":
            if tok == "]]" or ";" in tok:
                mode = "cmd"
            i += 1
            continue
        if tok == "[[" and not argv:
            mode = "test"
            i += 1
            continue
        if is_operator:
            if ">" in tok:
                op = tok[tok.index(">") - 1 :] if tok.index(">") > 0 else tok
                is_fd = ("&" in op or op.startswith("<")) and (nxt.isdigit() or nxt == "-")
                if not is_fd and not is_benign_redirect_target(nxt.translate(_RAW_UNESCAPE)):
                    targets.append(nxt.translate(_RAW_UNESCAPE))
                i += 2
                continue
            if "<" in tok:
                i += 2  # input redirect and its source
                continue
            if tok.startswith(")") and parents:
                parent, mode, depth = parents.pop()
                argvs.append(parent)
            else:
                argvs.append([])
            i += 1
            continue
        if tok.isdigit() and nxt and nxt[0] in "<>":
            i += 1  # fd number of a redirect, e.g. the `2` in `2>&1`
            continue
        word = tok.strip("`").lstrip("$").translate(_RAW_UNESCAPE)
        if tok != "$" and not (not argv and (word in _RAW_LEADING_KEYWORDS or _RAW_ASSIGNMENT.match(word))):
            argv.append(word)
        i += 1
    return argvs, targets


def check_unsafe_args_in_raw_command(command: str) -> Optional[str]:
    """Coarse argv and write-redirect checks for commands shell_parser can't parse.

    Uses both tree-sitter's best-effort tree and a shlex tokenization. Keeps
    commands that would be denied for a dangerous argument (`find -exec`,
    `sort -o`, ...) or a file write (`> file`) denied even when the full parser
    gives up. It errs toward flagging.

    Returns:
        A reason if the command appears to use such a primitive, None otherwise
    """
    try:
        tree_argvs, tree_targets = scan_for_deny_checks(command)
    except Exception:  # best effort only
        tree_argvs, tree_targets = [], []
    raw_argvs, raw_targets = _raw_argvs(command)
    targets = tree_targets + raw_targets
    if targets:
        return f"output redirection to '{targets[0]}' writes to the filesystem"
    for argv in tree_argvs + raw_argvs:
        reason = dangerous_argv_reason(argv)
        if reason:
            return reason
    return None


def match_prefix(segment: str, prefix: str) -> bool:
    """
    Check if a command segment matches a prefix.

    The prefix should match the beginning of the command at word boundaries.
    Accepts whitespace or '/' as valid boundaries (for kubectl resource/name syntax).

    Examples:
        - "kubectl get pods" matches prefix "kubectl get"
        - "kubectl delete pod" does NOT match prefix "kubectl get"
        - "grep -r error" matches prefix "grep"
        - "kubectl get secret/my-secret" matches prefix "kubectl get secret"
    """
    segment = segment.strip()
    prefix = prefix.strip()

    if not segment.startswith(prefix):
        return False

    # If prefix is shorter than segment, the next char must be boundary char or end
    if len(segment) > len(prefix):
        next_char = segment[len(prefix)]
        # Allow whitespace or path separator as boundary
        if not (next_char.isspace() or next_char == "/"):
            return False

    return True


def match_prefix_for_deny(segment: str, prefix: str) -> bool:
    """
    Check if a command segment matches a deny list prefix.

    More aggressive than allow list matching to prevent security bypasses:
    - Treats '/' as a valid boundary (catches 'kubectl get secret/name' syntax)
    - Also matches plural form (prefix + 's') to catch resource type aliases

    Examples:
        - "kubectl get secret/my-secret" matches prefix "kubectl get secret"
        - "kubectl get secrets" matches prefix "kubectl get secret" (plural)
        - "kubectl get secrets/my-secret" matches prefix "kubectl get secret"
    """
    segment = segment.strip()
    prefix = prefix.strip()

    def is_deny_boundary_char(char: str) -> bool:
        """Check if char is a valid boundary for deny matching."""
        return char.isspace() or char == "/"

    def check_at_boundary(seg: str, pref: str) -> bool:
        """Check if segment starts with prefix at a valid boundary."""
        if not seg.startswith(pref):
            return False
        if len(seg) > len(pref):
            if not is_deny_boundary_char(seg[len(pref)]):
                return False
        return True

    # Check exact prefix match
    if check_at_boundary(segment, prefix):
        return True

    # Check plural form (handles 'secret' matching 'secrets')
    if check_at_boundary(segment, prefix + "s"):
        return True

    return False


def _annotate_option_tokens(argv: List[str]) -> List[Tuple[str, bool]]:
    """Tag each argv token as an option (or an option's value) vs a positional.

    Positionals carry the command's verbs (`kubectl get`), so only they are
    compared against prefix words. Options are skipped wherever they appear.

    `--flag=value` and `-n` are self-contained. In the SEPARATED form
    (`--context prod`) the following word is the flag's VALUE, not a verb, and
    must be tagged too — otherwise `kubectl --context prod get` would compare
    `prod` against the prefix word `get` and fail.
    """
    annotated: List[Tuple[str, bool]] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("-"):
            annotated.append((token, True))
            # Separated form: consume the flag's value, which is not a verb.
            # `--flag=value` already carries its value, and a following token
            # that is itself an option starts a new flag rather than being a value.
            if "=" not in token and i + 1 < len(argv):
                following = argv[i + 1]
                if not following.startswith("-"):
                    annotated.append((following, True))
                    i += 1
        else:
            annotated.append((token, False))
        i += 1
    return annotated


def _match_argv_prefix(
    argv: List[str], prefix: str, match_plural: bool = False
) -> bool:
    """Flag-aware prefix match against a parsed argv.

    Every prefix word must match a positional token, consecutively and in
    order; the first non-matching positional fails the match. Options between,
    before or after the verbs are ignored.

    This is deliberately NOT a loose subsequence scan: such a scan would also
    match `kubectl delete pod get` against the prefix `kubectl get`, which is
    fine for checking that a prefix is not fabricated but unsafe as an allow
    gate.

    `match_plural` mirrors `match_prefix_for_deny`'s extra aggressiveness: the
    deny side also accepts the plural of the final prefix word, so a deny entry
    of `kubectl get secret` still blocks `kubectl get secrets`. It is off for
    allow matching, which must stay strict.
    """
    prefix_tokens = prefix.strip().split()
    if not prefix_tokens or not argv:
        return False

    idx = 0
    for token, is_option in _annotate_option_tokens(argv):
        if idx == len(prefix_tokens):
            break
        if is_option:
            continue

        want = prefix_tokens[idx]
        candidates = [want]
        if match_plural and idx == len(prefix_tokens) - 1:
            candidates.append(want + "s")

        # The final prefix word also matches kubectl's `resource/name` syntax,
        # so prefix `kubectl get secret` covers `kubectl get secret/my-secret`.
        if any(
            token == candidate
            or (idx == len(prefix_tokens) - 1 and token.startswith(candidate + "/"))
            for candidate in candidates
        ):
            idx += 1
            continue

        # A positional that is not the next expected prefix word means this is a
        # different command (`kubectl delete ...` vs prefix `kubectl get`).
        return False

    return idx == len(prefix_tokens)


def _best_effort_argvs(command: str) -> List[List[str]]:
    """Every simple command's argv, including inside a compound statement.

    `parse_command` REFUSES a compound statement (`for`/`while`/`if`), so on
    its own it returns nothing for exactly the commands that matter most here —
    a loop is the main reason a command needs approval in the first place.
    `scan_for_deny_checks` is the best-effort scanner that does see inside one.

    Its tree may be wrong, so by contract the result may only ever be used to
    DENY a command or to *clear* a prefix for further validation — NEVER as the
    basis for allowing execution. Both callers respect that:

    * the anti-fabrication check, where over-recovering an argv can only turn
      an unappealable DENIED into an APPROVAL_REQUIRED the user decides on, and
      every segment still faces the strict gates in `validate_segment`;
    * the deny pass for unparseable commands, which only ever denies.
    """
    try:
        return parse_command(command).command_argvs
    except ShellParseError:
        pass
    try:
        argvs, _targets = scan_for_deny_checks(command)
        return argvs
    except Exception:
        # The scanner is best-effort by contract; a failure here just means we
        # fall back to the string-based verdict.
        return []


def segment_argv(segment: str) -> Optional[List[str]]:
    """Parse a single segment and return its argv, or None if unavailable.

    Re-parsed from the segment rather than threaded in from `validate_command`
    so this never depends on `parsed.segments` and `parsed.command_argvs` being
    positionally parallel. They happen to be in this version — a bare
    assignment now raises rather than producing a segment with no argv — but
    that is an implementation detail of the parser, and zipping the two lists
    by index would compare a segment against another command's argv if it ever
    changed back.

    Returns None when the segment cannot be parsed or is not a single simple
    command, in which case callers fall back to the string-based matchers.
    """
    try:
        parsed = parse_command(segment)
    except ShellParseError:
        return None

    if len(parsed.command_argvs) != 1:
        # No argv, or more than one (the caller handed us something that is not
        # a single simple command): fall back to strings.
        return None

    return parsed.command_argvs[0]


def validate_segment(
    segment: str, allow_list: List[str], deny_list: List[str]
) -> ValidationResult:
    """
    Validate a single command segment against allow/deny lists.

    Validation order:
    1. Hardcoded blocks -> DENIED
    2. Deny list -> DENIED
    3. Allow list -> ALLOWED
    4. Neither -> APPROVAL_REQUIRED
    """
    # Step 1: Check hardcoded blocks
    blocked = check_hardcoded_blocks(segment)
    if blocked:
        return ValidationResult(
            status=ValidationStatus.DENIED,
            deny_reason=DenyReason.HARDCODED_BLOCK,
            message=f"Command contains '{blocked}' which is permanently blocked for security reasons and cannot be overridden.",
        )

    # Both prefix checks below are string-OR-argv. The string matchers are
    # `startswith` tests, so anything the shell puts before a command's verb
    # defeats them: an option (`kubectl --context=p get`) or an environment
    # assignment (`TZ=UTC date`). The argv matcher skips options and sees argv
    # with assignments already stripped, which fixes both with one mechanism.
    #
    # Additive, so path-based prefixes such as the auto-allowed
    # `cat <tool result storage>` keep working through the string matcher —
    # those are not argv verbs at all.
    argv = segment_argv(segment)

    # Step 2: Check deny list (using stricter matching)
    #
    # The deny side MUST get the same treatment as the allow side, and in the
    # same change: a flag-aware allow check alone would let
    # `kubectl --context=p logs x` slip past a `kubectl logs` deny entry,
    # turning a configured block into an approval prompt a user can wave
    # through. `match_plural` keeps the string matcher's extra aggressiveness.
    for deny_prefix in deny_list:
        if match_prefix_for_deny(segment, deny_prefix) or (
            argv is not None
            and _match_argv_prefix(argv, deny_prefix, match_plural=True)
        ):
            return ValidationResult(
                status=ValidationStatus.DENIED,
                deny_reason=DenyReason.DENY_LIST,
                message=f"Command matches deny list pattern '{deny_prefix}'. This command is blocked by configuration.",
            )

    # Step 3: Check allow list
    for allow_prefix in allow_list:
        if match_prefix(segment, allow_prefix) or (
            argv is not None and _match_argv_prefix(argv, allow_prefix)
        ):
            return ValidationResult(status=ValidationStatus.ALLOWED)

    # Step 4: Not in any list -> needs approval
    return ValidationResult(
        status=ValidationStatus.APPROVAL_REQUIRED,
        message=f"Command segment '{segment}' is not in the allow list.",
    )


def validate_command(
    command: str,
    suggested_prefixes: List[str],
    allow_list: List[str],
    deny_list: List[str],
) -> ValidationResult:
    """
    Validate a bash command against the allow/deny lists.

    Args:
        command: The full bash command to validate
        suggested_prefixes: AI-provided prefixes (one per command segment)
        allow_list: List of allowed command prefixes
        deny_list: List of denied command prefixes

    Returns:
        ValidationResult with status and details
    """
    # Hardcoded blocks come FIRST, before the fabrication check below.
    #
    # Both outcomes are DENIED, so this changes no decision — but it reports
    # the real one. `sudo kubectl --context=p get nodes` would otherwise be
    # denied as a fabricated prefix (`sudo` takes the first positional, so the
    # prefix `kubectl get` matches neither the string nor the argv), telling the
    # model to fix its prefixes when the actual problem is that `sudo` is
    # permanently blocked. The model then retries the same blocked command with
    # different prefixes instead of giving up.
    blocked_outright = check_blocked_in_raw_command(command, HARDCODED_BLOCKS)
    if blocked_outright:
        return ValidationResult(
            status=ValidationStatus.DENIED,
            deny_reason=DenyReason.HARDCODED_BLOCK,
            message=f"Command contains '{blocked_outright}' which is permanently blocked for security reasons and cannot be overridden.",
        )

    # Verify all suggested prefixes actually appear in the command.
    #
    # This is an anti-FABRICATION check — it only establishes that the model did
    # not invent a prefix for a command it is not running — so it is allowed to
    # be loose. It must NOT be strict: a false positive here is a DENIED, which
    # unlike APPROVAL_REQUIRED the user can never approve, so an ordinary
    # read-only command fails outright with no way forward.
    #
    # A plain substring test is too strict in exactly the case our config
    # produces, because an option can split the words of a prefix:
    #
    #     "kubectl get" not in "kubectl --context=staging get nodes"
    #
    # So fall back to the flag-aware argv match, which skips options. Checked
    # per simple command: a prefix belongs to ONE of the command's segments
    # (`kubectl get ... | grep x` suggests both "kubectl get" and "grep"), so a
    # prefix matching any argv is accounted for. Loose on purpose — the allow
    # and deny gates in validate_segment are the strict ones.
    fabrication_argvs: Optional[List[List[str]]] = None
    for prefix in suggested_prefixes:
        if prefix in command:
            continue
        if fabrication_argvs is None:
            fabrication_argvs = _best_effort_argvs(command)
        if any(
            _match_argv_prefix(argv, prefix, match_plural=True)
            for argv in fabrication_argvs
        ):
            continue
        return ValidationResult(
            status=ValidationStatus.DENIED,
            deny_reason=DenyReason.PREFIX_NOT_IN_COMMAND,
            message=f"Suggested prefix '{prefix}' does not appear in the command.",
        )

    # Parse command into segments and detect compound statements
    try:
        parsed = parse_command(command)
    except ShellParseError:
        # Can't parse — do safety checks on raw string, then ask user to approve
        blocked = check_blocked_in_raw_command(command, HARDCODED_BLOCKS)
        if blocked:
            return ValidationResult(
                status=ValidationStatus.DENIED,
                deny_reason=DenyReason.HARDCODED_BLOCK,
                message=f"Command contains '{blocked}' which is permanently blocked for security reasons and cannot be overridden.",
            )
        denied = check_blocked_in_raw_command(command, deny_list)
        if denied:
            return ValidationResult(
                status=ValidationStatus.DENIED,
                deny_reason=DenyReason.DENY_LIST,
                message=f"Command matches deny list pattern '{denied}'. This command is blocked by configuration.",
            )
        # The raw-string deny check above is flag-blind, exactly like
        # match_prefix_for_deny: `kubectl logs` does not appear in
        # `for p in a b; do kubectl --context=p logs $p; done`. This branch
        # handles the commands parse_command REFUSES — compound statements
        # among them — so without an argv pass a deny entry is unenforced in
        # precisely the commands that most need approval.
        #
        # scan_for_deny_checks exists for this: best-effort argvs that may only
        # be used to DENY, never to allow. That is all this does.
        for argv in _best_effort_argvs(command):
            for deny_prefix in deny_list:
                if _match_argv_prefix(argv, deny_prefix, match_plural=True):
                    return ValidationResult(
                        status=ValidationStatus.DENIED,
                        deny_reason=DenyReason.DENY_LIST,
                        message=f"Command matches deny list pattern '{deny_prefix}'. This command is blocked by configuration.",
                    )
        unsafe = check_unsafe_args_in_raw_command(command)
        if unsafe:
            return _unsafe_arg_result(unsafe, _unsafe_args_approval_mode())
        return ValidationResult(
            status=ValidationStatus.APPROVAL_REQUIRED,
            message="Command contains complex syntax which requires approval.",
            prefixes_needing_approval=[],
        )

    segments = parsed.segments
    contains_compound_command = parsed.contains_compound_command

    # Deny checks must also see the words bash will run, not only the quoted
    # source: `kubectl get 'secret'` must match deny entry `kubectl get secret`.
    for argv in parsed.command_argvs:
        result = validate_segment(" ".join(argv), [], deny_list)
        if result.status == ValidationStatus.DENIED:
            return result

    # Validate each segment against deny/allow lists
    unapproved_segments: List[str] = []

    for segment in segments:
        result = validate_segment(segment, allow_list, deny_list)

        # If any segment is denied, the whole command is denied
        if result.status == ValidationStatus.DENIED:
            return result

        if result.status == ValidationStatus.APPROVAL_REQUIRED:
            unapproved_segments.append(segment)

    # Argv-level security check: code-exec/write primitives and output
    # redirections that prefix matching cannot see (applies to every command
    # node, including those inside pipes/compound statements). Runs AFTER the
    # per-segment loop so a hardcoded-block / deny-list DENY there is never
    # pre-empted by an argv approval (e.g. the shell-expansion gate, or an
    # exec/write vector in approval mode).
    dangerous = check_dangerous_argv(parsed)
    if dangerous:
        return dangerous

    # Compound commands always require approval, even if all segments are allowed.
    # Only unapproved-segment approvals save prefixes to the allow list —
    # compound and unparseable approvals are one-time only.
    if contains_compound_command:
        return ValidationResult(
            status=ValidationStatus.APPROVAL_REQUIRED,
            message="Contains compound statements (for/while/if/etc).",
            prefixes_needing_approval=[],
        )

    if unapproved_segments:
        prefixes_needing_approval = list(
            dict.fromkeys(
                prefix
                for prefix in suggested_prefixes
                if not any(match_prefix(prefix, allowed) for allowed in allow_list)
            )
        )
        return ValidationResult(
            status=ValidationStatus.APPROVAL_REQUIRED,
            message=f"Segment(s) not in allow list: {', '.join(repr(s) for s in unapproved_segments)}",
            prefixes_needing_approval=prefixes_needing_approval,
        )

    # All segments validated and allowed
    return ValidationResult(status=ValidationStatus.ALLOWED)
