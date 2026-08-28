"""Tests for k7e recall (RAG) and _call_llm helper."""
import json
import os

import pytest

import engine
from conftest import write_path_executable


class TestRecallNoLLM:
    """Recall behavior when no LLM is available (most test environments)."""

    def test_empty_store_returns_nothing(self, store):
        answer, sources = engine.recall("anything at all")
        assert answer is None
        assert sources == []

    def test_empty_input_returns_nothing(self, store):
        answer, sources = engine.recall("")
        assert answer is None
        assert sources == []

    def test_whitespace_input_returns_nothing(self, store):
        answer, sources = engine.recall("   \n\t  ")
        assert answer is None
        assert sources == []

    def test_returns_sources_without_answer(self, store):
        """When nodes match but no LLM is available, sources are still returned."""
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.store_entry("Redis Persistence", "Redis supports RDB and AOF", tags=["redis"])
        answer, sources = engine.recall("redis port")
        # No LLM → no synthesis, but sources should be found
        assert answer is None
        assert len(sources) >= 1
        assert any("Redis" in s["title"] for s in sources)

    def test_unrelated_query_finds_nothing(self, store):
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        answer, sources = engine.recall("quantum chromodynamics gluon plasma")
        assert answer is None
        assert sources == []

    def test_limit_respected(self, store):
        for i in range(10):
            engine.store_entry(f"Fact {i}", f"Knowledge about topic number {i}", tags=["facts"])
        _, sources = engine.recall("knowledge topic", limit=3)
        assert len(sources) <= 3

    def test_long_input_searches_raw_text(self, store):
        """Long input without LLM: decompose returns nothing, so recall searches
        the raw text directly. Key assertion: no crash, sources is a list."""
        engine.store_entry("SSH Tunneling", "Use ssh -L for local port forwarding", tags=["ssh"])
        long_text = (
            "We were discussing SSH tunneling and port forwarding techniques "
            "for securely accessing services behind firewalls. The conversation "
            "covered both local and remote forwarding patterns."
        )
        _, sources = engine.recall(long_text)
        assert isinstance(sources, list)


class TestCallLlm:
    """Test _call_llm graceful failures."""

    def test_returns_none_when_no_command(self, store, monkeypatch):
        monkeypatch.delenv("K7E_LLM_COMMAND", raising=False)
        monkeypatch.delenv("K7E_SUMMARIZE_COMMAND", raising=False)
        result = engine._call_llm("test prompt", purpose="summarize")
        assert result is None

    def test_a_windows_path_survives_the_split(self):
        r"""`shlex.split` defaults to POSIX rules, where a backslash escapes
        the next character. Neither branch is reachable from the other's
        machine, so both are driven directly — including what the wrong one
        does, because that is the defect rather than a hypothetical."""
        given = r"C:\Users\me\AppData\fake-llm.py"
        assert engine.llm_argv(given, windows=True) == [given]
        assert engine.llm_argv(given, windows=False) == ["C:UsersmeAppDatafake-llm.py"]

    def test_a_quoted_windows_path_loses_its_quotes_and_keeps_its_spaces(self):
        r"""What `posix=False` gets wrong in the other direction: it saves the
        backslashes and then hands the quote characters through as part of the
        token, so the program looked up is `"C:\Program Files\llm.exe"`,
        quotes included."""
        assert engine.llm_argv(r'"C:\Program Files\llm.exe" --json', windows=True) == [
            r"C:\Program Files\llm.exe", "--json"
        ]

    def test_a_quoted_posix_bridge_still_reaches_the_shell_as_a_script(self):
        """The same defect with a different cost: a `sh -c` bridge whose quotes
        survive the split arrives as a command *name*, and the shell answers
        `No such file or directory` for the whole one-liner. Windows boxes have
        `sh` through Git Bash, so it half-runs rather than cleanly failing."""
        argv = engine.llm_argv(
            """sh -c 'cat >/dev/null; echo "[]"'""", windows=True
        )
        # argv[0] is whatever `sh` resolved to, which has its own tests.
        assert argv[1:] == ["-c", 'cat >/dev/null; echo "[]"']

    def test_a_single_quoted_backslash_is_not_doubled(self):
        r"""The hole the first two fixes both left. POSIX rules treat a
        backslash as literal inside SINGLE quotes, so pre-doubling the string
        is never undone there. Paths hide it — Win32 collapses repeated
        separators, so `C:\\\\Users` opens the same file as
        `C:\\Users` — and it only bites where a backslash is data."""
        assert engine.llm_argv(r"llm --re '\d+'", windows=True)[1:] == ["--re", r"\d+"]
        assert engine.llm_argv(r"llm --re " + '"' + r"\d+" + '"', windows=True)[1:] == [
            "--re", r"\d+"
        ]

    def test_a_single_quoted_path_keeps_its_separators(self):
        assert engine.llm_argv(r"python 'C:\tools\llm.py'", windows=True)[1:] == [
            r"C:\tools\llm.py"
        ]

    def test_an_apostrophe_in_a_windows_path_is_not_a_quote(self):
        r"""`C:\Users\O'Brien\llm.exe` is an ordinary profile name, and POSIX
        rules open a quote on it that never closes. Windows' own quoting has
        no single-quote form. The single-quoted grouping is still tried first,
        so a `sh -c '<script>'` bridge on a Git Bash box keeps it."""
        given = r"C:\Users\O'Brien\llm.exe"
        assert engine.llm_argv(given, windows=True) == [given]
        argv = engine.llm_argv(r"sh -c 'echo hi'", windows=True)
        assert argv[1:] == ["-c", "echo hi"], "the fallback must be second, not first"

    def test_an_apostrophe_still_opens_a_quote_on_posix(self):
        """The fallback is the platform's rule, not a general loosening: on
        POSIX an apostrophe IS a quote and an unbalanced one is an error."""
        with pytest.raises(ValueError):
            engine.llm_argv(r"/home/o'brien/llm", windows=False)

    def test_a_backslash_before_a_double_quote_is_an_escape(self):
        r"""The one place a backslash on Windows is not a separator. Turning
        the escape off entirely kept every path and lost this: a reviewer
        measured `python -c "print(\"hi\")"` coming out as `print(\hi\)` —
        quotes gone, backslashes kept, a JSON payload corrupted on its way to
        the bridge with nothing to report it."""
        assert engine.llm_argv(r'python -c "print(\"hi\")"', windows=True)[1:] == [
            "-c", 'print("hi")'
        ]
        assert engine.llm_argv(r'llm --json "{\"key\":\"value\"}"', windows=True)[1:] == [
            "--json", '{"key":"value"}'
        ]

    def test_the_backslash_run_before_a_quote_follows_the_platform_rule(self):
        r"""`2n` backslashes before a `"` are `n` and the quote quotes; `2n+1`
        are `n` and the quote is literal. That is what `CommandLineToArgvW`
        does, so it is what the program on the other end will parse with, and a
        run NOT before a quote is left exactly as written — the boundary of the
        rule, and the reason every path survives it —
        anything else means the two sides disagree about the same string."""
        assert engine.llm_argv(r'llm "a\\b"', windows=True)[1:] == [r"a\\b"]
        assert engine.llm_argv(r'llm "a\"b"', windows=True)[1:] == ['a"b']
        assert engine.llm_argv(r'llm "a\\\"b"', windows=True)[1:] == [r'a\"b']
        assert engine.llm_argv(r'llm "C:\tools\\"', windows=True)[1:] == ["C:\\tools\\"]

    def test_an_unterminated_quote_is_an_error_on_windows_too(self):
        r"""`"C:\tools\"` reads as an escaped quote and therefore never closes.

        Raising is a DIVERGENCE from the platform, not agreement with it:
        `CommandLineToArgvW` tolerates a quote left open at the end. This parser
        recovers intent from a setting rather than emulating that one, and a
        config string that quietly becomes `C:\tools"` is the failure this area
        keeps producing. `"C:\tools\\"` is how the path is written."""
        with pytest.raises(ValueError, match="No closing quotation"):
            engine.llm_argv(r'"C:\tools\"', windows=True)

    def test_the_escape_rule_is_off_inside_single_quotes(self):
        r"""Single-quote grouping exists for a `sh -c '<script>'` bridge, and
        in the shell that form belongs to a backslash inside it is literal.
        Applying the Windows rule there would rewrite the script."""
        argv = engine.llm_argv(r"""sh -c 'echo "a\"b"'""", windows=True)
        assert argv[1:] == ["-c", r'echo "a\"b"']

    def test_a_group_opens_and_closes_only_at_a_token_boundary(self):
        r"""The rule that makes an apostrophe a character again.

        Windows quoting has no single-quote form. `'` grouping exists here only
        so a `sh -c '<script>'` bridge survives on a box that has a shell, so it
        may only OPEN a group where a token begins and only CLOSE one where a
        token ends. Anywhere else it is what it is on that platform: a letter in
        a name.

        Both halves were found by a reviewer, and both deleted characters out of
        a path or a message rather than failing."""
        # Opening: two apostrophes inside a path balanced, read as grouping, and
        # both vanished — `C:\Users\OBriens\llm.exe`.
        for given in (
            r"C:\Users\O'Brien's\llm.exe",
            r"C:\a'b'c'd",
            r"C:\bin#1\O'Brien.exe",
            r"C:\tools\'weird.exe",
            r"C:\tools\weird'.exe",
        ):
            assert engine.llm_argv(given, windows=True) == [given]
        assert engine.llm_argv(
            r"C:\Users\D'Angelo's Tools\llm.exe --json", windows=True
        ) == [r"C:\Users\D'Angelo's", r"Tools\llm.exe", "--json"]

        # The shape that needs the OPENING half specifically. Where a mid-token
        # group would close at a boundary, nothing raises and no fallback runs:
        # the apostrophes are deleted and the space between the words is
        # swallowed, silently, into one token.
        assert engine.llm_argv(r"llm C:\a'b c'", windows=True)[1:] == [
            r"C:\a'b", "c'"
        ]

        # Closing: the apostrophe in `won't` closed the group and was deleted,
        # so one argument became two and a character went missing.
        assert engine.llm_argv(r"""--msg 'it won't parse'""", windows=True) == [
            "--msg", "it won't parse"
        ]
        assert engine.llm_argv(
            r"""sh -c 'python C:\Users\O'Brien\llm.py'""", windows=True
        )[1:] == ["-c", r"python C:\Users\O'Brien\llm.py"]

        # And the bridge the grouping exists for is untouched.
        assert engine.llm_argv(
            """sh -c 'cat >/dev/null; echo "[]"'""", windows=True
        )[1:] == ["-c", 'cat >/dev/null; echo "[]"']

    def test_a_double_quote_still_toggles_anywhere_in_a_token(self):
        r"""The boundary rule is for `'` alone. `a"b c"d` is ordinary Windows
        quoting and has to keep toggling mid-token, so the two quote characters
        cannot share one rule."""
        assert engine.llm_argv(r'llm a"b c"d', windows=True)[1:] == ["ab cd"]

    def test_a_doubled_quote_inside_a_quoted_string_is_one_literal_quote(self):
        r"""The platform's other escape, and the only one that needs no
        backslash. A doubled JSON payload is written `"{""key"":""value""}"`;
        without the rule the pairs cancel and every quote disappears from the
        value, which reaches the bridge as `{key:value}` and parses as nothing."""
        assert engine.llm_argv(r'llm --json "{""key"":""value""}"', windows=True)[1:] == [
            "--json", '{"key":"value"}'
        ]
        assert engine.llm_argv(r'llm "a""b"', windows=True)[1:] == ['a"b']

    def test_a_group_that_never_closes_is_still_refused(self):
        r"""What the token-edge check is left guarding. With the boundary rule
        in place the strings it was written for — `--msg 'it won't parse'` and
        the `sh -c` bridge with a profile name in it — parse as what the
        operator wrote, so they never reach it. A group opened at a boundary and
        never closed has no reading at all, and that still gets the loud answer.

        The price is unchanged and still recorded: a bare program name beginning
        with an apostrophe is refused, where the same file written
        `C:\tools\'weird.exe` runs."""
        for given in ("'weird.exe --x", "llm '", "sh -c 'echo hi"):
            with pytest.raises(ValueError, match="unbalanced apostrophe"):
                engine.llm_argv(given, windows=True)

    def test_a_command_that_cannot_be_parsed_is_a_failure_not_a_skip(
        self, store, monkeypatch, capsys
    ):
        """The severity is the exit code, not the parse. `distill` catches
        ValueError per file on purpose — one damaged capture must not wedge a
        sweep — so a parse error escaping into that catch skipped every file
        in a directory while exiting 0, and a sweep reads 0 as a successful
        dream and advances its watermark past captures nothing ever read."""
        monkeypatch.setenv("K7E_LLM_COMMAND", 'llm --note "unclosed')
        engine._llm_failures.clear()
        assert engine._call_llm("test", purpose="summarize") is None
        assert engine._llm_failures, "the run has to hear about it"
        assert "cannot be parsed" in engine._llm_failures[0][1]

    def test_an_unquoted_hash_is_part_of_the_argument(self):
        """`shlex.split` clears `commenters`; a hand-built lexer has to too, or
        a `#` starts a comment and the rest of the command silently vanishes.
        Quoted is not the case that discriminates — a versioned directory is."""
        given = r"C:\tools\bin#1\llm.exe --json"
        assert engine.llm_argv(given, windows=True) == [
            r"C:\tools\bin#1\llm.exe", "--json"
        ]
        assert engine.llm_argv("llm --note a#b", windows=False)[1:] == ["--note", "a#b"]

    def test_a_bare_program_name_is_resolved_to_its_path(self, tmp_path, monkeypatch):
        """Windows appends only `.exe` to a bare name and never `.cmd`, which
        is how an npm-installed CLI arrives. `shutil.which` matches PATHEXT.

        Compared through `normcase` because PATHEXT is spelled in upper case,
        so `which` returns `pretend-llm.CMD` for a file written as `.cmd`. The
        path is right and usable — Windows paths are case-insensitive — and a
        bare `==` would be asserting about spelling rather than about which
        file was found."""
        target = write_path_executable(tmp_path, "pretend-llm", "pass\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        program, flag = engine.llm_argv("pretend-llm --json")
        assert os.path.normcase(program) == os.path.normcase(str(target))
        assert flag == "--json"

    def test_a_name_that_resolves_to_nothing_is_left_for_the_os(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert engine.llm_argv("no-such-llm")[0] == "no-such-llm"

    def test_a_path_is_never_re_resolved(self, tmp_path):
        given = str(tmp_path / "llm.py")
        assert engine.llm_argv(given) == [given]

    def test_the_models_output_is_read_as_utf8(self, store, monkeypatch, tmp_path):
        """`text=True` alone decodes through the platform's code page, which on
        Windows is not UTF-8. A model that writes an em-dash — and they all do —
        would come back mojibake or raise."""
        script = write_path_executable(tmp_path, "unicode-llm", (
            "import sys\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "sys.stdin.read()\n"
            "sys.stdout.write('a \\u2014 b \\u2192 c')\n"
        ))
        monkeypatch.setenv("K7E_LLM_COMMAND", str(script))
        assert engine._call_llm("test", purpose="summarize") == "a \u2014 b \u2192 c"

    def test_returns_none_on_timeout(self, store, monkeypatch, tmp_path):
        script = write_path_executable(
            tmp_path, "slow", "import time\ntime.sleep(5)\n"
        )
        monkeypatch.setenv("K7E_LLM_COMMAND", str(script))
        result = engine._call_llm("test", purpose="summarize", timeout=1)
        assert result is None

    def test_strips_ansi_cursor_control_from_output(self, store, monkeypatch, tmp_path):
        """ollama run word-wraps at 80 columns even when piped, splicing cursor-
        control sequences (e.g. \\x1b[1D\\x1b[K) mid-token into stdout (#77)."""
        script = write_path_executable(tmp_path, "ansi", (
            "import sys\n"
            "sys.stdout.write('[{\"title\": \"Fo\\x1b[1D\\x1b[Koo\", "
            "\"content\": \"bar\"}]')\n"
        ))
        monkeypatch.setenv("K7E_LLM_COMMAND", str(script))
        result = engine._call_llm("test", purpose="summarize")
        assert "\x1b" not in result
        assert json.loads(result) == [{"title": "Fooo", "content": "bar"}]

    def test_strips_csi_tilde_sequence(self):
        """ESC[3~ (e.g. Delete key) has a non-alpha final byte; the old
        alpha-only final-byte class left a raw ESC behind."""
        assert engine._strip_ansi("a\x1b[3~b") == "ab"

    def test_strips_osc_title_terminated_by_bel(self):
        """OSC (ESC]) title-setting sequences terminate with BEL, not a CSI
        final byte, and must be consumed in full."""
        assert engine._strip_ansi("\x1b]0;window title\x07prompt$ ") == "prompt$ "

    def test_strips_osc_title_terminated_by_st(self):
        """OSC sequences may also terminate with ST (ESC \\) instead of BEL."""
        assert engine._strip_ansi("\x1b]0;window title\x1b\\prompt$ ") == "prompt$ "


class TestDecomposeQueries:
    """Without an LLM, _decompose_queries returns [] (no word-split fallback);
    recall() then searches the raw text."""

    def test_returns_empty_without_llm(self, store):
        queries = engine._decompose_queries("short text only four words here extra padding needed")
        assert queries == []

    def test_empty_returns_empty(self, store):
        queries = engine._decompose_queries("")
        assert queries == []


class TestRecallCLI:
    """CLI recall fails fast when no LLM command is configured."""

    def test_recall_fails_fast_without_llm(self, store, monkeypatch, capsys):
        import io
        import cli

        monkeypatch.setattr("sys.stdin", io.StringIO("redis port forwarding"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        exit_code = cli.main(["recall"])
        assert exit_code == 1
        assert "requires an LLM" in capsys.readouterr().err

    def test_recall_with_text_arg_fails_fast(self, store, capsys):
        import cli
        engine.store_entry("Test Node", "Some test content for recall", tags=["test"])
        exit_code = cli.main(["recall", "test content"])
        assert exit_code == 1
        assert "requires an LLM" in capsys.readouterr().err
