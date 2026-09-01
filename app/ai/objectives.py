"""What this turn has to accomplish.

`sales_agent.md` describes *how* to sell. This module decides *what to do now*,
and the answer is handed to the model as a single instruction for the turn.

The ladder is built for the channel it actually runs in: someone tapped an ad on
Instagram and landed in WhatsApp knowing almost nothing. That person will not
answer "what is your role and what team are you on" - they will leave. So the
order is **value first, questions later, and never a dead end**:

  1. say what it is and what it saves them
  2. connect one concrete outcome to whatever they said back
  3. get the download in front of them - it is free, so the ask is tiny, and it
     is the only thing on this ladder that can actually convert
  4. handle the blocker, bridge to a specific later moment

Position in that sequence is measured by how many replies we have sent, not by
how many facts we have collected. Gating on collected facts is what produced an
interrogation: role, then team, then challenges, and a customer who answered
"nothing" and was shown the door.

Pure functions only: state in, a sentence out. Nothing here touches the database
or the model, which is what makes the sales ladder cheap to test and to change.
"""

from __future__ import annotations

from app.domain import SalesStage, normalise_platform

#: Facts worth remembering when the customer volunteers them. They personalise
#: the pitch; they are never prerequisites for making it. `platform` decides
#: which installer the link points at, so it is asked alongside the download -
#: never as a condition of getting it.
QUALIFICATION_SLOTS: tuple[str, ...] = ("use_case", "role", "platform")

#: Stages where there is nothing left to advance towards, so no objective is
#: issued and the prompt's ordinary behaviour applies.
_NO_OBJECTIVE_STAGES = frozenset(
    {
        SalesStage.NOT_INTERESTED,
        SalesStage.HUMAN_HANDOFF,
        SalesStage.DOWNLOADED,
        SalesStage.ACTIVATED,
        SalesStage.CLOSED,
    }
)

_OPENING = (
    "They have just arrived, almost certainly from an ad, and know next to nothing "
    "about Boomshare. Do NOT ask what they do, what team they are on, or what "
    "problems they have - nobody answers that on WhatsApp and it reads like a form. "
    "Greet them like a person, say in one plain sentence what Boomshare is and the "
    "one thing it saves them - recording a quick video instead of typing a long "
    "explanation or booking a call - and mention it is free to start. Then ask one "
    "easy question about what they would use it for. Under 40 words."
)

_CONNECT = (
    "They have engaged. Take whatever they just told you and connect it to one "
    "concrete outcome Boomshare gives them - the result, not the feature list. If "
    "they gave you nothing to work with, do not dig: offer the most common use "
    "instead, like sending a two-minute walkthrough rather than writing three "
    "paragraphs. Then offer to send the download so they can try it themselves. "
    "It is free, so the ask is small - make it."
)

_NUDGE = (
    "They are still browsing and have not committed. Do not interrogate them, and "
    "never end on a dead end - a reply with no next step is exactly where people "
    "leave the conversation. Give them one more concrete reason it is worth two "
    "minutes, and offer the link directly. If they say they have no need for it, do "
    "not argue and do not give up: name a benefit they have not thought of, and "
    "leave the offer open."
)

_SEND_LINK = (
    "They want it. Hand the download over now by requesting the send_download_link "
    "action - do not make them ask twice and do not qualify them first. Tell them "
    "what happens next: the installer takes about a minute, then they record from "
    "the toolbar icon. Do not write the link yourself - the backend attaches it."
)

_SEND_LINK_UNKNOWN_PLATFORM = (
    _SEND_LINK
    + " You do not know yet whether they are on Windows or a Mac, so ask that in "
    "the same message - the link goes with it either way. Never hold the download "
    "back for an answer."
)

_UNSHIPPABLE_PLATFORM = (
    "They want it, but they have told you they are on something Boomshare does "
    "not ship for - a phone, or another operating system. Do not send a desktop "
    "installer to someone who cannot run it. Say plainly which machines it needs, "
    "then ask whether they have one they could use. If they name a moment when "
    "they will - even 'in five minutes' - request the schedule_follow_up action "
    "with follow_up_minutes set to exactly that, and say you will check back then."
)

_POST_LINK = (
    "The link is already with them. Your only job now is getting them to a first "
    "recording. Do not pitch again, do not re-offer the link, and do not restate "
    "the features. Ask what is holding them back, or help with the step they are "
    "stuck on."
)

_OBJECTION = (
    "They have raised a real blocker. Do not argue with it and do not soften the "
    "facts. Acknowledge it plainly, then pivot to the benefit they would still get, "
    "and bridge to a specific later moment - request the schedule_follow_up action "
    "with follow_up_minutes set to the time they actually named, so the "
    "conversation is picked back up exactly when they expect it."
)


def missing_slots(notes: dict[str, str] | None) -> list[str]:
    """Facts we have not learned yet, in the order they become useful.

    `platform` is judged by whether it names a build we actually ship, not by
    whether the model wrote something. A customer who says "mobile" has answered
    the question but is still not installable, and treating that as known would
    release a desktop link to someone holding a phone.
    """
    known = notes or {}
    missing = [slot for slot in QUALIFICATION_SLOTS if not str(known.get(slot) or "").strip()]
    if "platform" not in missing and normalise_platform(known.get("platform")) is None:
        missing.append("platform")
    return missing


def next_objective(
    stage: SalesStage,
    notes: dict[str, str] | None,
    *,
    download_link_sent: bool = False,
    replies_sent: int = 0,
) -> str | None:
    """The one thing this turn should achieve, or None to leave the model alone.

    `replies_sent` is how many messages we have already sent in this
    conversation. It is what places us on the ladder: nobody is ready to be sold
    to on the first reply, and nobody wants to be qualified on the fourth.
    """
    if stage in _NO_OBJECTIVE_STAGES:
        return None

    if download_link_sent or stage == SalesStage.LINK_SENT:
        return _POST_LINK

    if stage == SalesStage.OBJECTION_HANDLING:
        return _OBJECTION

    if stage == SalesStage.DOWNLOAD_SUGGESTED:
        # They have signalled they want it, so the link goes out this turn -
        # not knowing which build changes what else the message says, never
        # whether the download is in it. The one exception is a customer who
        # has named a platform we do not ship for: a desktop installer is no
        # use on a phone, and sending it anyway reads as not having listened.
        if "platform" not in missing_slots(notes):
            return _SEND_LINK
        if (notes or {}).get("platform"):
            return _UNSHIPPABLE_PLATFORM
        return _SEND_LINK_UNKNOWN_PLATFORM

    if replies_sent == 0:
        return _OPENING
    if replies_sent == 1:
        return _CONNECT
    return _NUDGE
