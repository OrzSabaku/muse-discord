"""Paced REST rotations and sliced background jobs, independent of the monitor.

One thread alternates polling and maintenance within a shared time budget.
Network operations and clocks are supplied explicitly; a backoff pauses polling
and discovery, and ends the reconciliation pass without publishing unknown work.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from muse_discord.conversation import channel_display

POLL_SPREAD = 0.5


@dataclass(frozen=True)
class RestSchedule:
    poll_interval: float
    sweep_every: int
    channel_refresh: int
    thread_refresh: int = 5
    slice_seconds: float = 0.4
    spread: float = POLL_SPREAD


@dataclass(frozen=True)
class RestClock:
    now: Callable[[], float]
    backing_off: Callable[[], bool]


@dataclass(frozen=True)
class RestOperations:
    channels: Callable[[], dict]
    identity_ready: Callable[[], bool]
    poll_targets: Callable[[], list]
    list_dm_channels: Callable[[], list]
    check_dm_channel: Callable[[str, str, dict], list]
    check_channel: Callable[..., Any]
    load_queue: Callable[[], list]
    sweep_mentions: Callable[..., bool]
    publish_queue_additions: Callable[[dict, list], None]
    save_state: Callable[[dict], None]
    discovery_iter: Callable[[dict], Iterator]
    thread_refresh_iter: Callable[[dict], Iterator]


@dataclass(frozen=True)
class RestContext:
    config: RestSchedule
    clock: RestClock
    operations: RestOperations


@dataclass
class DiscoveryRefresh:
    pending: bool = False

    def request(self):
        self.pending = True


class RestJob(Protocol):
    name: str

    def done(self) -> bool: ...

    def run_slice(self, state, deadline, queue_ok) -> int: ...


def run_phase(label, work, *args):
    """A broken phase must not skip sends, cleanup, or later heartbeats."""
    try:
        return work(*args)
    except Exception as exc:
        print(f"{label} error: {exc}", flush=True)
        return None


class PollRotation:
    """One pass of the REST poll over its targets, done in slices."""

    def __init__(self, targets, started, context: RestContext, spread=POLL_SPREAD):
        self.context = context
        self.targets = list(targets)
        self.pos = 0
        self.started = started
        self.spread = spread

    def done(self):
        return self.pos >= len(self.targets)

    def next_due(self):
        """When the next target is due on the pacing schedule."""
        return self.started + (
            self.context.config.poll_interval * self.spread * self.pos / max(1, len(self.targets))
        )

    def due(self, now):
        return not self.done() and now >= self.next_due()

    def run_slice(self, state, deadline, dms_out):
        """Poll due targets until deadline, at least one. Polled guild
        triggers are staged inside (stage_found); DMs are appended to
        dms_out for the staging phase. Returns the number of targets
        polled."""
        n = 0
        while self.due(self.context.clock.now()) and not self.context.clock.backing_off():
            if n and self.context.clock.now() >= deadline:
                break
            target = self.targets[self.pos]
            self.pos += 1
            if self._poll(state, target, dms_out):
                n += 1
        return n

    def _poll(self, state, target, dms_out):
        if target[0] == "dms":
            # DMs: every message not from the bot is addressed to the
            # bot. Resolving the channels is a step; each DM channel is
            # polled as the next ones.
            self.targets[self.pos : self.pos] = [
                ("dm", dm_id, dm_name)
                for dm_id, dm_name in self.context.operations.list_dm_channels()
            ]
            return True
        if target[0] == "dm":
            _, dm_id, dm_name = target
            dms_out.extend(
                (m, "DM with " + dm_name)
                for m in self.context.operations.check_dm_channel(dm_id, dm_name, state)
            )
            return True
        cid = target[1]
        info = self.context.operations.channels().get(cid)
        if info is None:
            return False  # unmonitored since the rotation began
        cname = channel_display(info)
        gid = info["guild_id"]
        try:
            # Triggers are staged inside, in arrival order.
            self.context.operations.check_channel(cid, cname, state, gid)
        except Exception as e:
            print(f"Poll error in {cname}: {e}", flush=True)
        return True


class SweepPass:
    """One reconciliation sweep pass (sweep_mentions), done in slices: a
    step sweeps one channel (or the owners' DM channels), each against
    the durable records as they are at that moment."""

    name = "Sweep"

    def __init__(self, channel_ids, context: RestContext):
        self.context = context
        self.targets = [None, *channel_ids]  # None: the DMs
        self.pos = 0

    def done(self):
        return self.pos >= len(self.targets)

    def run_slice(self, state, deadline, queue_ok):
        if not queue_ok:
            # The queue could not be read this tick: the sweep must not
            # write it. The pass ends, as a skipped sweep did.
            self.pos = len(self.targets)
            return 0
        queue = self.context.operations.load_queue()  # after this tick's sends' removals
        n_before = len(queue)
        changed = False
        n = 0
        while not self.done():
            if self.context.clock.backing_off():
                # A 429 (here or elsewhere) ends the pass: the sweep must
                # not add request pressure (sweep_mentions logs it).
                self.pos = len(self.targets)
                break
            if n and self.context.clock.now() >= deadline:
                break
            cid = self.targets[self.pos]
            self.pos += 1
            if cid is None:
                changed |= self.context.operations.sweep_mentions(state, queue, {})
            elif cid in self.context.operations.channels():  # (else unmonitored since it began)
                changed |= self.context.operations.sweep_mentions(
                    state, queue, {cid: self.context.operations.channels()[cid]}, dms=False
                )
            else:
                continue
            n += 1
        if changed:
            self.context.operations.publish_queue_additions(state, queue[n_before:])
            self.context.operations.save_state(state)
        return n


class GeneratorJob:
    """A background REST job written as a generator that yields after
    each request (thread refresh, discovery)."""

    def __init__(self, name, gen, clock: RestClock):
        self.clock = clock
        self.name = name
        self.gen = gen
        self.finished = False

    def done(self):
        return self.finished

    def run_slice(self, state, deadline, queue_ok):
        n = 0
        while not self.finished and not self.clock.backing_off():
            if n and self.clock.now() >= deadline:
                break
            try:
                next(self.gen)
            except StopIteration:
                self.finished = True
                break
            n += 1
        return n


class RestScheduler:
    """Alternate polling and background work within one REST slice budget."""

    def __init__(self, context: RestContext, refresh: DiscoveryRefresh):
        self.context = context
        self.refresh = refresh
        self.cycle = 0
        self.rotation: PollRotation | None = None
        self.rotation_started = None
        self.backoff_seen = False
        self.jobs: list[RestJob] = []
        self.poll_turn_next = True
        self.cycle_done = False

    def begin_tick(self, now, backing):
        self.cycle_done = False
        self.backoff_seen = self.backoff_seen or backing
        if (
            self.rotation is None
            and not backing
            and (
                self.rotation_started is None
                or now - self.rotation_started >= self.context.config.poll_interval
            )
        ):
            self.rotation = PollRotation(
                self.context.operations.poll_targets(),
                now,
                self.context,
                spread=0.0 if self.rotation_started is None else self.context.config.spread,
            )
            self.rotation_started = now

    def poll_slice(self, state, now, backing, dm_items):
        poll_ready = self.rotation is not None and self.rotation.due(now) and not backing
        jobs_ready = bool(self.jobs) and not backing
        poll_turn = poll_ready and (self.poll_turn_next or not jobs_ready)
        if poll_ready and jobs_ready:
            self.poll_turn_next = not self.poll_turn_next
        spent = 0.0
        if poll_turn:
            started = self.context.clock.now()
            if self.rotation is not None:
                run_phase(
                    "Poll",
                    self.rotation.run_slice,
                    state,
                    started + self.context.config.slice_seconds,
                    dm_items,
                )
            spent = self.context.clock.now() - started
        if self.rotation is not None and self.rotation.done():
            self.rotation = None
            self.cycle += 1
            self.cycle_done = True
        return poll_turn, spent

    def recovery_cycle(self):
        recovery = self.backoff_seen if self.cycle_done else False
        if self.cycle_done:
            self.backoff_seen = self.context.clock.backing_off()
        return recovery

    def schedule_jobs(self, state, recovery):
        if not self.cycle_done:
            return
        kinds = {job.name for job in self.jobs}
        if (
            self.cycle % self.context.config.sweep_every == 0
            and self.context.operations.identity_ready()
            and "Sweep" not in kinds
        ):
            self.jobs.append(SweepPass(list(self.context.operations.channels()), self.context))
        if (
            self.cycle % self.context.config.channel_refresh == 0 or self.refresh.pending
        ) and not recovery:
            self.refresh.pending = False
            if "Channel refresh" not in kinds:
                self.jobs.append(
                    GeneratorJob(
                        "Channel refresh",
                        self.context.operations.discovery_iter(state),
                        self.context.clock,
                    )
                )
        elif (
            self.cycle % self.context.config.thread_refresh == 0
            and not recovery
            and not kinds & {"Thread refresh", "Channel refresh"}
        ):
            self.jobs.append(
                GeneratorJob(
                    "Thread refresh",
                    self.context.operations.thread_refresh_iter(state),
                    self.context.clock,
                )
            )

    def run_job(self, state, poll_turn, rest_spent, queue_known):
        if not self.jobs:
            return
        if self.context.clock.backing_off():
            if isinstance(self.jobs[0], SweepPass):
                self.jobs.pop(0)
            return
        job = self.jobs[0]
        try:
            if not poll_turn or rest_spent < self.context.config.slice_seconds:
                job.run_slice(
                    state,
                    self.context.clock.now() + self.context.config.slice_seconds - rest_spent,
                    queue_known,
                )
            if job.done():
                self.jobs.pop(0)
        except Exception as exc:
            print(f"{job.name} error: {exc}", flush=True)
            self.jobs.pop(0)

    def next_wait(self, now, backing):
        """REST's idle allowance; callers can shorten it for incoming work."""
        if backing:
            return 2.0
        if self.jobs or (self.rotation is not None and self.rotation.due(now)):
            return 0.0
        if self.rotation is not None:
            return min(2.0, max(1.0, self.rotation.next_due() - now))
        if self.rotation_started is not None:
            return min(
                2.0, max(0.0, self.context.config.poll_interval - (now - self.rotation_started))
            )
        return 2.0
