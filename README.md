# awask — your agent asks you a question, and acts on your answer

An agent that needs a human has one channel today: prose in a terminal. That channel
loses. The deciding sentence sits in paragraph four, you are in another window, and
the run sits idle for forty minutes because nothing told you it was waiting.

`awask` makes the ask a **thing** instead of a line of text: a small structured card
in a durable store, rendered by every surface, whose answer is carried back into the
run that raised it.

```bash
pip install awask
```

Python 3.9+. **Zero dependencies** — standard library only. The store is a directory
of JSON files, so this is the entire setup: no server, no login, no network, nothing
to start before it works.

## Ask something

```bash
awask ask "Ship the migration now, or hold for review?" \
  --summary "Tests pass. The rollback path is untested." \
  --fact "312 rows affected" \
  --fact "rollback has never been run against prod data" \
  --option "ship|Ship it|live in ~2 min, rollback unproven" \
  --option "hold|Hold for review|no risk, blocks tonight's release" \
  --recommend hold --default hold --urgency high
```

Returns immediately. Your agent keeps working; you answer at your own pace.

```bash
awask list                      # what is waiting on you
awask show <id>                 # one card in full
awask answer <id> hold          # pick an option
awask steer <id> "check the FK constraint first"   # guidance; card stays open
awask cancel <id>               # withdraw it — you solved it yourself
```

Use `|` as the option separator, never `:` — a Windows path in a consequence
(`C:\data`) silently mangles with the colon form.

## Close the loop in Claude Code

The CLI alone gives you cards when an agent types the command, and delivers your
answer at your next prompt. To make it automatic:

```bash
awask install-hooks          # this project
awask install-hooks --user   # every project
awask install-hooks --dry-run
```

That writes three hooks and merges three entries into `settings.json`:

| event | hook | what it does |
|---|---|---|
| `Stop` | `stop_awask_cards.py` | holds the turn open ~50s so your answer resumes the run |
| `UserPromptSubmit` | `awask_mailbox_drain.py` | **carries your answer INTO the session** |
| `Notification` | `awask_notification_card.py` | "the agent is waiting" becomes a card |

**The middle one is the one people skip and the one that matters.** Without it you can
answer a card and the agent never learns you did — the card renders, you click, the
store records a resolution, and the run carries on as though nobody replied. Every
component reports success.

Your existing `settings.json` is **merged, never replaced**; re-running is a no-op;
a malformed settings file is refused rather than overwritten; and every hook is run
with `--self-test` after being written, because a copy is not an install.

## The five fields that make a card decidable

A card that cannot be answered from a phone lock screen is not finished.

1. **Title** — one line, the whole ask. If it needs two, raise two cards.
2. **Facts** — what you *measured*, not what you guess. Highest-value field here.
3. **Options** — each with a **consequence**. A label says what a choice is called; a
   consequence says what happens to your machine. Only the consequence lets someone
   decide without reading the code.
4. **`--recommend`** — say what you would do. A card with no recommendation pushes
   your thinking onto the human.
5. **`--default`** — what happens if nobody ever answers. **Required**: a card is only
   safe to ignore if it says what ignoring it does. An agent that cannot name a
   default is not blocked on a decision, it is blocked on doing its own thinking.

## When to raise one — the bar is high

**A card is an interruption. The default is to keep working.** Raise one only when
all three hold:

1. The answers lead to **materially different work**, and
2. **A wrong guess costs real work** to undo — time, money, data, or something
   outward-facing, and
3. **You cannot settle it yourself** by reading, measuring, or taking the
   conventional default.

If any clause fails: decide it, say so in one line, keep going. "Should I continue?"
is the anti-pattern.

## From Python

```python
from awask import DecisionCard, DecisionOption, get_store

card = get_store().create(DecisionCard(
    title="Two schema designs, both defensible",
    options=[
        DecisionOption(key="a", label="Single table", consequence="faster reads, wide rows"),
        DecisionOption(key="b", label="Join table", consequence="normalized, extra hop"),
    ],
    recommend="b", default="b",
))
print(card.id)
```

## Answering from elsewhere

`awask list/show/answer/cancel` read the store on local disk directly, so a fresh
install works with nothing running. Reaching those cards from another device — a
phone, a browser, a dashboard — needs a small local HTTP daemon serving the same
store, bound to loopback and fronted by a private tunnel or an authenticated proxy.
Never publish that port: it serves the queue that steers an agent with filesystem
access.

**Nothing starts that daemon for you, deliberately** — registering a background
service is a bigger ask than installing a CLI. The cost is worth naming: if it is
not running, remote surfaces have no cards to show, and an empty list looks exactly
like "nothing is waiting on you". A client that cannot reach it should say so.

## Delivery to a chat DM

`awask.channels` bridges cards to a Discord/Telegram/Slack **direct message** — your
bot, your token, bound to your user id. It is a DM bridge, not a channel bot, and
that is the entire security model: **a card answer steers an agent with filesystem
access**, so "who may answer" is an authorization decision. It fails closed at every
path — no config, no bound owner, a sender who is not the owner, or a message that
is not in a DM, all deny. A message in a public channel cannot answer a card even
when you typed it.

## Licence

Apache 2.0.
