"""The triggers pages (pivot P4, task 8).

One :class:`PageSpec` per page of the section: triggers (the four kinds and
their CRUD), subscriptions (what a fire does), firing a trigger now (and the
webhook call from outside) and waiting for events (every tool that pauses a
turn). The first, second and last pages carry the drift guards for the
trigger kinds, the subscription kinds and the pausing tools.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("triggers/triggers", "Triggers",
             "Create, read, change, enable and delete triggers: delayed, scheduled, webhook and channel.",
             "feature", 1,
             tools=scoped("crud", "create_trigger update_trigger") + scoped("trigger", "list get create update delete"),
             related=("triggers/subscriptions", "triggers/fire-now", "channels/bindings"),
             max_chars=12000),
    PageSpec("triggers/subscriptions", "Subscriptions",
             "A subscription says what a trigger fire does: start an agent or graph session, steer a session or resume a parked one.",
             "feature", 2,
             tools=scoped("trigger", "list_subscriptions get_subscription create_subscription update_subscription delete_subscription"),
             related=("triggers/triggers", "triggers/waiting-for-events", "sessions/start-a-session")),
    PageSpec("triggers/fire-now", "Fire a trigger now",
             "Fire a trigger on demand to test it, and how webhook triggers are called from outside.",
             "feature", 3,
             tools=scoped("trigger", "fire_now"),
             related=("triggers/triggers", "triggers/subscriptions"),
             must=("X-Primer-Signature",)),
    PageSpec("triggers/waiting-for-events", "Waiting for events",
             "Pause a turn until an event, a trigger or a channel message arrives: which tools pause, and how they resume.",
             "feature", 4,
             tools=scoped("system", "wait_for_event") + scoped("workspace_ext", "subscribe_to_trigger subscribe_to_channel_event"),
             related=("human-in-the-loop/ask-user", "utilities/sleep-and-watch-files", "sessions/messaging-other-sessions"),
             max_chars=12000),
)
