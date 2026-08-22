import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeClock:
    """Stepped virtual clock: callables read .now, tests move it."""

    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


import signal

import pytest

from rotator import Settings, ShutdownState


class FakeChunks:
    """Stand-in for SendResult.body()'s generator: iterable + closable."""

    def __init__(self, items):
        self._items = list(items)
        self.closed = False

    def __iter__(self):
        return iter(self._items)

    def close(self):
        self.closed = True


from rotator import TERMINAL_SSE_EVENT, guarded_stream


class TestGuardedStream:
    def test_not_armed_passes_chunks_through_verbatim(self):
        state = ShutdownState(clock=FakeClock())
        chunks = FakeChunks([b"a", b"b"])
        assert list(guarded_stream(chunks, state=state)) == [b"a", b"b"]
        # Natural exhaustion still releases the inner iterable.
        assert chunks.closed

    def test_window_elapsing_midstream_appends_terminal_event_and_closes(self):
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(20.0)
        chunks = FakeChunks([b"a", b"b"])

        def slow_clock_moves_at_second_chunk():
            yield b"a"
            yield b"b"
            clock.now = 120.0
            yield b"never reached"

        out = list(guarded_stream(slow_clock_moves_at_second_chunk(), state=state))
        assert out == [b"a", b"b", TERMINAL_SSE_EVENT]

    def test_already_draining_cuts_before_first_chunk(self):
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(0.0)
        chunks = FakeChunks([b"a"])
        assert list(guarded_stream(chunks, state=state)) == [TERMINAL_SSE_EVENT]
        assert chunks.closed

    def test_closing_guard_early_closes_inner(self):
        state = ShutdownState(clock=FakeClock())
        chunks = FakeChunks([b"a", b"b"])
        gen = guarded_stream(chunks, state=state)
        next(gen)
        gen.close()
        assert chunks.closed


class TestStreamDrainWindowSetting:
    def test_defaults_to_20_seconds(self, monkeypatch):
        monkeypatch.delenv("STREAM_DRAIN_WINDOW", raising=False)
        assert Settings.from_env().stream_drain_window == 20.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("STREAM_DRAIN_WINDOW", "1.5")
        assert Settings.from_env().stream_drain_window == 1.5

    def test_zero_means_cut_on_next_chunk(self, monkeypatch):
        monkeypatch.setenv("STREAM_DRAIN_WINDOW", "0")
        assert Settings.from_env().stream_drain_window == 0.0


class TestShutdownState:
    def test_not_armed_never_drains(self):
        clock = FakeClock()
        state = ShutdownState(clock=clock)
        assert not state.draining()

    def test_armed_within_window_is_not_draining(self):
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(20.0)
        clock.now = 119.9
        assert not state.draining()

    def test_armed_past_window_is_draining(self):
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(20.0)
        clock.now = 120.0
        assert state.draining()

    def test_arm_is_idempotent_first_deadline_wins(self):
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(20.0)
        clock.now = 110.0
        state.arm(50.0)
        clock.now = 120.5
        assert state.draining()

    def test_zero_grace_drains_immediately_after_arm(self):
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(0.0)
        assert state.draining()


class TestInflightTracking:
    def test_started_and_finished_balance(self):
        state = ShutdownState(clock=FakeClock())
        assert state.inflight == 0
        state.stream_started()
        assert state.inflight == 1
        state.stream_finished()
        assert state.inflight == 0

    def test_guard_tracks_stream_lifecycle(self):
        state = ShutdownState(clock=FakeClock())
        gen = guarded_stream(FakeChunks([b"a", b"b"]), state=state)
        next(gen)
        assert state.inflight == 1
        gen.close()
        assert state.inflight == 0


class SignalSpy:
    """In-memory stand-in for signal.getsignal/signal.signal."""

    def __init__(self):
        self.handlers = {}

    def get(self, sig):
        return self.handlers.get(sig, signal.SIG_DFL)

    def set(self, sig, handler):
        self.handlers[sig] = handler


def make_installer(spy):
    from rotator import install_shutdown_handlers
    return lambda *a, **kw: install_shutdown_handlers(
        *a, get_signal=spy.get, set_signal=spy.set, **kw
    )


class TestInstallShutdownHandlers:
    def test_installs_term_and_int(self):
        spy = SignalSpy()
        installer = make_installer(spy)
        installer(ShutdownState(clock=FakeClock()), 20.0)
        assert signal.SIGTERM in spy.handlers
        assert signal.SIGINT in spy.handlers

    def test_handler_arms_state_with_grace(self):
        spy = SignalSpy()
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        make_installer(spy)(state, 20.0)
        spy.handlers[signal.SIGTERM](signal.SIGTERM, None)
        clock.now = 100.5
        assert not state.draining()
        clock.now = 120.0
        assert state.draining()

    def test_callable_previous_handler_is_delegated_after_arm(self):
        spy = SignalSpy()
        calls = []
        spy.handlers[signal.SIGTERM] = lambda sig, frame: calls.append(sig)
        state = ShutdownState(clock=FakeClock())
        make_installer(spy)(state, 20.0)
        spy.handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert calls == [signal.SIGTERM]
        assert state._deadline is not None

    def test_default_prev_exits_once_inflight_drains(self):
        spy = SignalSpy()
        exits = []
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)

        def sleeper(seconds):
            state.stream_finished()  # guard releases while watchdog waits

        from rotator import install_shutdown_handlers
        make_installer(spy)(state, 20.0,
                            exit_fn=lambda code: exits.append(code),
                            sleeper=sleeper)
        state.stream_started()
        spy.handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert exits == [0]
        assert state._deadline is not None

    def test_default_prev_deadline_forces_exit_despite_inflight(self):
        spy = SignalSpy()
        exits = []
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)

        def sleeper(seconds):
            clock.now += 30.0  # jump past the drain window

        from rotator import install_shutdown_handlers
        install_shutdown_handlers(
            state, 20.0, get_signal=spy.get, set_signal=spy.set,
            exit_fn=lambda code: exits.append(code), sleeper=sleeper,
        )
        state.stream_started()
        spy.handlers[signal.SIGINT](signal.SIGINT, None)
        assert exits == [0]

    def test_window_elapsed_with_inflight_gives_guards_a_settle_beat(self):
        """Draining flips while a stream is still open: the watchdog must not
        exit before a guard observes it between chunks and flushes the
        terminal event."""
        spy = SignalSpy()
        exits = []
        sleeps = []
        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)

        def sleeper(seconds):
            sleeps.append(seconds)

        from rotator import install_shutdown_handlers
        make_installer(spy)(state, 0.0,  # draining immediately
                            exit_fn=lambda code: exits.append(code),
                            sleeper=sleeper)
        state.stream_started()
        spy.handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert exits == [0]
        assert 0.25 in sleeps

    def test_install_is_idempotent(self):
        spy = SignalSpy()
        installer = make_installer(spy)
        state = ShutdownState(clock=FakeClock())
        installer(state, 20.0)
        first = spy.handlers[signal.SIGTERM]
        installer(state, 20.0)
        assert spy.handlers[signal.SIGTERM] is first


class TestViewWiring:
    def test_armed_state_cuts_stream_with_terminal_event(self, mock, client, monkeypatch):
        import rotator as rotator_module
        from rotator import TERMINAL_SSE_EVENT, ShutdownState

        clock = FakeClock(now=100.0)
        state = ShutdownState(clock=clock)
        state.arm(0.0)
        monkeypatch.setattr(rotator_module, "shutdown_state", state)

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.data == TERMINAL_SSE_EVENT

    def test_unarmed_state_passes_stream_through(self, mock, client, monkeypatch):
        import rotator as rotator_module
        from rotator import ShutdownState

        monkeypatch.setattr(
            rotator_module, "shutdown_state", ShutdownState(clock=FakeClock())
        )
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert b"[DONE]" in resp.data
        assert b"proxy_shutdown" not in resp.data

    def test_create_app_builds_shutdown_state_and_installs_handlers(self, rotator, monkeypatch):
        import rotator as rotator_module
        from rotator import ShutdownState

        assert isinstance(rotator_module.shutdown_state, ShutdownState)
